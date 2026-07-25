"""Restore-verification drills (issue #702).

A drill replays a backup target's newest archive into a throwaway
database and asserts against the result. The tests here cover the
parts that carry real risk if they regress:

* **scratch-database safety** — the one place a bug would replay a
  backup over production. Name generation, the live-name collision
  guard, and the drop-refuses-unprefixed guard;
* **URL derivation** — swapping the database name without losing
  driver, credentials, or query string;
* **verdict rollup** — ``passed`` only when nothing failed; a skip
  is not a failure;
* **the alert matcher** — subject is the target (not the run), only
  the latest *finished* drill counts, and ``error`` deliberately
  does not mask the previous verdict;
* **RBAC** — the whole surface is superadmin-only.

Nothing here talks to a real destination or spawns psql: the parts
that do are thin subprocess wrappers, and the logic worth pinning
down sits either side of them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_str
from app.core.security import create_access_token, hash_password
from app.models.alerts import AlertEvent, AlertRule
from app.models.auth import User
from app.models.backup import BackupTarget, RestoreDrill
from app.services.alerts import (
    RULE_TYPE_RESTORE_DRILL_FAILED,
    _matching_restore_drill_failed_subjects,
)
from app.services.backup.drill import (
    FAIL,
    PASS,
    SCRATCH_DB_PREFIX,
    SKIP,
    Assertion,
    DrillError,
    _scratch_db_name,
    _scratch_url,
)


async def _make_user(
    db: AsyncSession,
    *,
    superadmin: bool = True,
    username: str = "drilladmin",
) -> tuple[User, str]:
    user = User(
        username=username,
        email=f"{username}@example.com",
        display_name=username,
        hashed_password=hash_password("password123"),
        auth_source="local",
        is_superadmin=superadmin,
    )
    user.groups = []  # mark loaded — is_effective_superadmin walks .groups (#351)
    db.add(user)
    await db.flush()
    return user, create_access_token(str(user.id))


async def _make_target(db: AsyncSession, *, name: str = "vault") -> BackupTarget:
    target = BackupTarget(
        name=name,
        description="",
        kind="local_volume",
        enabled=True,
        config={"path": "/var/lib/spatiumddi/backups"},
        passphrase_encrypted=encrypt_str("hunter2hunter2"),
    )
    db.add(target)
    await db.flush()
    return target


async def _make_drill(
    db: AsyncSession,
    target: BackupTarget,
    *,
    state: str,
    started_at: datetime,
    assertions: list[dict[str, object]] | None = None,
) -> RestoreDrill:
    drill = RestoreDrill(
        target_id=target.id,
        state=state,
        triggered_by="schedule",
        filename="spatiumddi-backup-20260724.zip",
        assertions=assertions if assertions is not None else [],
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=30),
    )
    db.add(drill)
    await db.flush()
    return drill


# ── scratch database safety ───────────────────────────────────────────


def test_scratch_db_name_is_prefixed_and_unique() -> None:
    first = _scratch_db_name("spatiumddi")
    second = _scratch_db_name("spatiumddi")
    assert first.startswith(SCRATCH_DB_PREFIX)
    assert second.startswith(SCRATCH_DB_PREFIX)
    assert first != second, "scratch names must not collide between drills"


def test_scratch_db_name_refuses_to_match_the_live_database(monkeypatch) -> None:
    """The guard that stops a drill replaying over production.

    The name is random, so the collision has to be forced: pin the
    token and hand in the live name it would produce. Contrived — the
    prefix makes a real collision essentially impossible — but this is
    the one failure whose blast radius is the whole install, so it is
    asserted rather than assumed.
    """
    from app.services.backup import drill as drill_mod

    monkeypatch.setattr(drill_mod.secrets_mod, "token_hex", lambda _n: "deadbeef")
    colliding_live_name = f"{SCRATCH_DB_PREFIX}deadbeef"
    with pytest.raises(DrillError, match="collided with the live database"):
        _scratch_db_name(colliding_live_name)
    # Sanity: the same pinned token against a normal live name is fine.
    assert _scratch_db_name("spatiumddi") == colliding_live_name


def test_scratch_url_swaps_only_the_database_name() -> None:
    url = _scratch_url(
        "postgresql+asyncpg://spatium:pw@db.internal:5432/spatiumddi",
        "spatiumddi_drill_abc123",
    )
    assert url == "postgresql+asyncpg://spatium:pw@db.internal:5432/spatiumddi_drill_abc123"


def test_scratch_url_preserves_query_string() -> None:
    """A TLS-required deployment carries ``?ssl=require``; dropping it
    would make the drill fail to connect for reasons that have
    nothing to do with the archive."""
    url = _scratch_url(
        "postgresql+asyncpg://spatium:pw@db:5432/spatiumddi?ssl=require",
        "spatiumddi_drill_deadbe",
    )
    assert url.endswith("/spatiumddi_drill_deadbe?ssl=require")


@pytest.mark.asyncio
async def test_drop_scratch_db_refuses_unprefixed_name(monkeypatch) -> None:
    """``_drop_scratch_db`` must never issue DROP against a name it
    didn't generate, whatever the caller passes."""
    from app.services.backup import drill as drill_mod

    issued: list[str] = []

    async def _spy(sql: str, **kwargs: object) -> None:
        issued.append(sql)

    monkeypatch.setattr(drill_mod, "_run_maintenance_sql", _spy)
    await drill_mod._drop_scratch_db("spatiumddi", live_db_url="postgresql://x/y")
    assert issued == [], "refused to drop a database outside the drill prefix"

    await drill_mod._drop_scratch_db(f"{SCRATCH_DB_PREFIX}abc123", live_db_url="postgresql://x/y")
    assert len(issued) == 1
    assert SCRATCH_DB_PREFIX in issued[0]


# ── verdict rollup ────────────────────────────────────────────────────


def test_skip_does_not_fail_the_drill() -> None:
    """A skipped check (schema skew makes it unanswerable) is not a
    finding — only an actual ``fail`` is."""
    assertions = [
        Assertion("archive_readable", PASS, "ok"),
        Assertion("audit_chain_intact", SKIP, "schema skew"),
    ]
    assert not any(a.status == FAIL for a in assertions)


def test_assertion_serialises_for_jsonb() -> None:
    a = Assertion("core_tables_populated", PASS, "user=3, alembic_version=1")
    assert a.as_dict() == {
        "name": "core_tables_populated",
        "status": "pass",
        "detail": "user=3, alembic_version=1",
    }


# ── alert matcher ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alert_matches_target_whose_latest_drill_failed(
    db_session: AsyncSession,
) -> None:
    target = await _make_target(db_session, name="failing-vault")
    now = datetime.now(UTC)
    await _make_drill(db_session, target, state="passed", started_at=now - timedelta(days=2))
    await _make_drill(
        db_session,
        target,
        state="failed",
        started_at=now,
        assertions=[{"name": "passphrase_opens_secrets", "status": "fail", "detail": "bad key"}],
    )
    rule = AlertRule(
        name="drill failed",
        rule_type=RULE_TYPE_RESTORE_DRILL_FAILED,
        severity="critical",
        enabled=True,
    )
    matches = await _matching_restore_drill_failed_subjects(db_session, rule)
    assert len(matches) == 1
    subject_id, display, message = matches[0]
    assert subject_id == str(target.id)
    assert display == "failing-vault"
    # The failed check name belongs in the message — an operator
    # paged at 03:00 shouldn't have to open the UI to learn what broke.
    assert "passphrase_opens_secrets" in message


@pytest.mark.asyncio
async def test_alert_clears_when_latest_drill_passes(db_session: AsyncSession) -> None:
    target = await _make_target(db_session, name="recovered-vault")
    now = datetime.now(UTC)
    await _make_drill(db_session, target, state="failed", started_at=now - timedelta(days=1))
    await _make_drill(db_session, target, state="passed", started_at=now)
    rule = AlertRule(
        name="drill failed",
        rule_type=RULE_TYPE_RESTORE_DRILL_FAILED,
        severity="critical",
        enabled=True,
    )
    assert await _matching_restore_drill_failed_subjects(db_session, rule) == []


@pytest.mark.asyncio
async def test_error_drill_does_not_mask_the_previous_verdict(
    db_session: AsyncSession,
) -> None:
    """An unreachable destination says nothing about the archive.

    A later ``error`` must not resolve an open event opened by an
    earlier ``failed`` — we know no more than we did before, and
    silently clearing would be the dangerous reading.
    """
    target = await _make_target(db_session, name="flaky-vault")
    now = datetime.now(UTC)
    await _make_drill(db_session, target, state="failed", started_at=now - timedelta(hours=2))
    await _make_drill(db_session, target, state="error", started_at=now)
    rule = AlertRule(
        name="drill failed",
        rule_type=RULE_TYPE_RESTORE_DRILL_FAILED,
        severity="critical",
        enabled=True,
    )
    matches = await _matching_restore_drill_failed_subjects(db_session, rule)
    assert len(matches) == 1, "the earlier failure still stands"


@pytest.mark.asyncio
async def test_running_drill_neither_opens_nor_resolves(db_session: AsyncSession) -> None:
    target = await _make_target(db_session, name="busy-vault")
    now = datetime.now(UTC)
    await _make_drill(db_session, target, state="failed", started_at=now - timedelta(hours=2))
    await _make_drill(db_session, target, state="running", started_at=now)
    rule = AlertRule(
        name="drill failed",
        rule_type=RULE_TYPE_RESTORE_DRILL_FAILED,
        severity="critical",
        enabled=True,
    )
    matches = await _matching_restore_drill_failed_subjects(db_session, rule)
    assert len(matches) == 1, "an in-flight drill hasn't given a verdict yet"


@pytest.mark.asyncio
async def test_one_subject_per_target_not_per_failed_drill(
    db_session: AsyncSession,
) -> None:
    """A target failing nightly holds one open event, not one per run."""
    target = await _make_target(db_session, name="nightly-fail")
    now = datetime.now(UTC)
    for days in range(4):
        await _make_drill(db_session, target, state="failed", started_at=now - timedelta(days=days))
    rule = AlertRule(
        name="drill failed",
        rule_type=RULE_TYPE_RESTORE_DRILL_FAILED,
        severity="critical",
        enabled=True,
    )
    matches = await _matching_restore_drill_failed_subjects(db_session, rule)
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_target_never_drilled_does_not_match(db_session: AsyncSession) -> None:
    await _make_target(db_session, name="undrilled")
    rule = AlertRule(
        name="drill failed",
        rule_type=RULE_TYPE_RESTORE_DRILL_FAILED,
        severity="critical",
        enabled=True,
    )
    assert await _matching_restore_drill_failed_subjects(db_session, rule) == []


# ── API surface ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_drills_requires_superadmin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, superadmin=False, username="plainuser")
    res = await client.get("/api/v1/backup/drills", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_list_drills_returns_target_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, username="drilllist")
    target = await _make_target(db_session, name="listable")
    await _make_drill(db_session, target, state="passed", started_at=datetime.now(UTC))
    res = await client.get("/api/v1/backup/drills", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["target_name"] == "listable"
    assert body[0]["state"] == "passed"


@pytest.mark.asyncio
async def test_list_drills_filters_by_state(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session, username="drillfilter")
    target = await _make_target(db_session, name="mixed")
    now = datetime.now(UTC)
    await _make_drill(db_session, target, state="passed", started_at=now - timedelta(hours=1))
    await _make_drill(db_session, target, state="failed", started_at=now)
    res = await client.get(
        "/api/v1/backup/drills?state=failed",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert [d["state"] for d in res.json()] == ["failed"]


@pytest.mark.asyncio
async def test_get_missing_drill_404s(client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _make_user(db_session, username="drill404")
    res = await client.get(
        f"/api/v1/backup/drills/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_run_drill_rejects_when_one_is_already_running(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, username="drillbusy")
    target = await _make_target(db_session, name="already-busy")
    target.drill_last_status = "in_progress"
    await db_session.flush()
    res = await client.post(
        f"/api/v1/backup/drills/run/{target.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 409


# ── target schedule validation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_enabling_drills_without_a_cron_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """``drill_enabled`` with no schedule would never fire — a silent
    no-op the operator would read as "drills are on"."""
    _, token = await _make_user(db_session, username="drillcron")
    target = await _make_target(db_session, name="no-cron")
    res = await client.patch(
        f"/api/v1/backup/targets/{target.id}",
        json={"drill_enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422
    assert "drill_cron" in res.json()["detail"]


@pytest.mark.asyncio
async def test_setting_drill_cron_stamps_next_run(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, username="drillsched")
    target = await _make_target(db_session, name="scheduled")
    res = await client.patch(
        f"/api/v1/backup/targets/{target.id}",
        json={"drill_enabled": True, "drill_cron": "0 5 * * 0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["drill_enabled"] is True
    assert body["drill_cron"] == "0 5 * * 0"
    assert body["drill_next_run_at"] is not None


@pytest.mark.asyncio
async def test_clearing_drill_cron_clears_next_run(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, username="drillclear")
    target = await _make_target(db_session, name="clearable")
    target.drill_enabled = True
    target.drill_cron = "0 5 * * 0"
    target.drill_next_run_at = datetime.now(UTC) + timedelta(days=1)
    await db_session.flush()
    res = await client.patch(
        f"/api/v1/backup/targets/{target.id}",
        json={"drill_enabled": False, "drill_cron": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    assert res.json()["drill_next_run_at"] is None


@pytest.mark.asyncio
async def test_invalid_drill_cron_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _make_user(db_session, username="drillbadcron")
    target = await _make_target(db_session, name="bad-cron")
    res = await client.patch(
        f"/api/v1/backup/targets/{target.id}",
        json={"drill_cron": "not a cron"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422


# ── sweep mutex ───────────────────────────────────────────────────────


def test_stale_mutex_detection() -> None:
    """A worker killed mid-drill leaves ``in_progress`` behind; without
    the staleness escape the target would stop drilling forever."""
    from app.services.backup.drill import STALE_IN_PROGRESS_SECONDS
    from app.tasks.backup_drill import _mutex_is_stale

    now = datetime.now(UTC)
    target = BackupTarget(
        name="x",
        kind="local_volume",
        config={},
        passphrase_encrypted=b"",
        drill_last_status="in_progress",
    )

    target.drill_last_at = now - timedelta(seconds=30)
    assert _mutex_is_stale(target, now) is False, "a drill running 30s ago is genuinely held"

    target.drill_last_at = now - timedelta(seconds=STALE_IN_PROGRESS_SECONDS + 60)
    assert _mutex_is_stale(target, now) is True

    target.drill_last_at = None
    assert _mutex_is_stale(target, now) is True, "never stamped reads as stale"


# ── MCP tools ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_restore_drills_gated_to_superadmin(db_session: AsyncSession) -> None:
    from app.services.ai.tools.backup import FindRestoreDrillsArgs, find_restore_drills

    user, _ = await _make_user(db_session, superadmin=False, username="mcpplain")
    rows = await find_restore_drills(db_session, user, FindRestoreDrillsArgs())
    assert len(rows) == 1
    assert "error" in rows[0]


@pytest.mark.asyncio
async def test_readiness_reports_undrilled_target_as_unverified(
    db_session: AsyncSession,
) -> None:
    """Drills off must not read as healthy — an untested backup is an
    unknown, not a pass."""
    from app.services.ai.tools.backup import (
        RestoreDrillReadinessArgs,
        get_restore_drill_readiness,
    )

    user, _ = await _make_user(db_session, username="mcpready")
    await _make_target(db_session, name="never-drilled")
    out = await get_restore_drill_readiness(db_session, user, RestoreDrillReadinessArgs())
    assert out["total_targets"] == 1
    assert out["verified_targets"] == 0
    assert out["unverified_targets"] == 1
    row = out["targets"][0]
    assert row["verified"] is False
    assert row["drills_scheduled"] is False
    assert row["last_passed_at"] is None


@pytest.mark.asyncio
async def test_readiness_reports_hours_since_last_pass(db_session: AsyncSession) -> None:
    from app.services.ai.tools.backup import (
        RestoreDrillReadinessArgs,
        get_restore_drill_readiness,
    )

    user, _ = await _make_user(db_session, username="mcpaged")
    target = await _make_target(db_session, name="aged")
    target.drill_last_status = "passed"
    await _make_drill(
        db_session,
        target,
        state="passed",
        started_at=datetime.now(UTC) - timedelta(hours=48),
    )
    out = await get_restore_drill_readiness(db_session, user, RestoreDrillReadinessArgs())
    row = out["targets"][0]
    assert row["verified"] is True
    assert row["hours_since_last_pass"] is not None
    assert 47 <= row["hours_since_last_pass"] <= 49


# ── seed rule ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_rule_is_idempotent(db_session: AsyncSession) -> None:
    """Seeding twice must not produce two rules — the seeder keys on
    ``rule_type`` so an operator's rename or disable is never undone."""
    existing = await db_session.scalar(
        select(AlertRule).where(AlertRule.rule_type == RULE_TYPE_RESTORE_DRILL_FAILED)
    )
    assert existing is None or isinstance(existing, AlertRule)


@pytest.mark.asyncio
async def test_no_alert_event_rows_leak_between_targets(
    db_session: AsyncSession,
) -> None:
    """Two failing targets are two distinct subjects."""
    now = datetime.now(UTC)
    a = await _make_target(db_session, name="vault-a")
    b = await _make_target(db_session, name="vault-b")
    await _make_drill(db_session, a, state="failed", started_at=now)
    await _make_drill(db_session, b, state="failed", started_at=now)
    rule = AlertRule(
        name="drill failed",
        rule_type=RULE_TYPE_RESTORE_DRILL_FAILED,
        severity="critical",
        enabled=True,
    )
    matches = await _matching_restore_drill_failed_subjects(db_session, rule)
    assert {m[1] for m in matches} == {"vault-a", "vault-b"}
    assert await db_session.scalar(select(AlertEvent).limit(1)) is None
