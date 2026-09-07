"""Cross-setting lockout guard (#1013).

Two independent source restrictions — the Web UI allow-list (#285 Phase 6)
and the SSH allow-list (#1009) — each shipped with an anti-lockout guard that
could only see its own door. So an operator could pass both, one at a time,
and end up reachable through neither. These tests pin the escalation that
now sees both, the acknowledgement it demands, and the fail direction the two
guards had been disagreeing about.

The ASGI transport reports the caller as 127.0.0.1; sending ``X-Real-IP``
overrides that, which is also what discriminates the trusted client-IP helper
from the spoofable one.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.auth import User
from app.models.feature_module import FeatureModule
from app.models.settings import PlatformSettings
from app.services.appliance.access import SSH, WEB_UI, covers, effective_doors
from app.services.feature_modules import invalidate_cache

FW = "/api/v1/appliance/firewall"
SETTINGS = "/api/v1/settings"

#: A valid ed25519 public key, so an SSH save can carry an unrelated edit.
_PUBKEY = "ssh-ed25519 " "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f test"


@pytest.fixture(autouse=True)
def _reset_module_cache():
    invalidate_cache()
    yield
    invalidate_cache()


async def _admin(db: AsyncSession, ip: str | None = None) -> dict:
    u = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@x.com",
        display_name="T",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    h = {"Authorization": f"Bearer {create_access_token(str(u.id))}"}
    if ip:
        h["X-Real-IP"] = ip
    return h


# ── the primitive ───────────────────────────────────────────────────


def test_an_address_we_cannot_read_is_not_covered() -> None:
    """The direction the two guards used to disagree about.

    Web UI counted an unreadable address as excluded (warn); SSH counted it
    as covered (proceed). One rule now, and it is the conservative one: a
    door we cannot confirm admits you is not a door.
    """
    assert covers(None, ["10.0.0.0/8"]) is False
    assert covers("", ["10.0.0.0/8"]) is False
    assert covers("not-an-ip", ["10.0.0.0/8"]) is False


def test_families_do_not_cross() -> None:
    """What nftables will do with the same pair."""
    assert covers("2001:db8::1", ["10.0.0.0/8"]) is False
    assert covers("10.1.2.3", ["2001:db8::/32"]) is False
    assert covers("2001:db8::1", ["2001:db8::/32"]) is True


def test_a_malformed_entry_is_skipped_not_fatal() -> None:
    """A bad entry cannot make the caller covered, so failing the whole
    check on one would turn a stale row into a warning on every save."""
    assert covers("10.1.2.3", ["nonsense", "10.0.0.0/8"]) is True
    assert covers("10.1.2.3", ["nonsense"]) is False


def test_an_unrestricted_door_admits_even_an_unreadable_caller() -> None:
    """Open means open. The conservative reading of an unknown address
    applies to membership of a list, not to the absence of one."""
    report = effective_doors(None, None)
    assert report.web_ui.admits and not report.web_ui.restricted
    assert report.ssh.admits and not report.ssh.restricted
    assert report.console_only is False


# ── the resolver ────────────────────────────────────────────────────


def test_ssh_is_unrestricted_until_lockdown_is_on() -> None:
    """Mirrors ``effective_ssh_scope``: a configured allow-list restricts
    nothing until the flag turns it into enforcement."""
    cfg = PlatformSettings(id=1, ssh_allowed_source_networks=["10.0.0.0/8"], ssh_lockdown=False)
    report = effective_doors(cfg, "203.0.113.9")
    assert report.ssh.restricted is False
    assert report.ssh.admits is True
    assert report.console_only is False


def test_console_only_needs_both_doors_shut() -> None:
    cfg = PlatformSettings(
        id=1,
        web_ui_allowed_cidrs=["10.0.0.0/8"],
        ssh_allowed_source_networks=["10.0.0.0/8"],
        ssh_lockdown=True,
    )
    inside = effective_doors(cfg, "10.1.2.3")
    assert inside.admitting == (WEB_UI, SSH)
    assert inside.console_only is False

    outside = effective_doors(cfg, "203.0.113.9")
    assert outside.admitting == ()
    assert outside.console_only is True


def test_one_door_open_is_not_console_only() -> None:
    cfg = PlatformSettings(
        id=1,
        web_ui_allowed_cidrs=["203.0.113.0/24"],
        ssh_allowed_source_networks=["10.0.0.0/8"],
        ssh_lockdown=True,
    )
    report = effective_doors(cfg, "203.0.113.9")
    assert report.admitting == (WEB_UI,)
    assert report.console_only is False


def test_overrides_describe_the_resulting_state_not_the_stored_one() -> None:
    """What makes one resolver usable from both write paths."""
    cfg = PlatformSettings(id=1, web_ui_allowed_cidrs=[], ssh_lockdown=False)
    assert effective_doors(cfg, "203.0.113.9").console_only is False
    after = effective_doors(
        cfg,
        "203.0.113.9",
        web_ui_cidrs=["10.0.0.0/8"],
        ssh_lockdown=True,
        ssh_cidrs=["10.0.0.0/8"],
    )
    assert after.console_only is True
    # …and the stored row is untouched by asking.
    assert cfg.web_ui_allowed_cidrs == []


# ── the SSH write path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ssh_lockdown_that_closes_the_last_door_is_escalated(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(PlatformSettings(id=1, web_ui_allowed_cidrs=["10.0.0.0/8"]))
    await db_session.commit()

    r = await client.put(
        SETTINGS,
        headers=h,
        json={"ssh_lockdown": True, "ssh_allowed_source_networks": ["10.0.0.0/8"]},
    )
    assert r.status_code == 422, r.text
    assert "last remote way in" in r.text
    assert "acknowledge_console_only" in r.text
    row = await db_session.get(PlatformSettings, 1)
    await db_session.refresh(row)
    assert row.ssh_lockdown is not True


@pytest.mark.asyncio
async def test_the_per_door_override_does_not_satisfy_the_escalation(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The whole point of a second acknowledgement: ``ssh_lockdown_force``
    accepts losing SSH while another door remains, which is a materially
    smaller statement and may have been sent for an unrelated reason."""
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(PlatformSettings(id=1, web_ui_allowed_cidrs=["10.0.0.0/8"]))
    await db_session.commit()

    r = await client.put(
        SETTINGS,
        headers=h,
        json={
            "ssh_lockdown": True,
            "ssh_allowed_source_networks": ["10.0.0.0/8"],
            "ssh_lockdown_force": True,
        },
    )
    assert r.status_code == 422, r.text
    assert "last remote way in" in r.text


@pytest.mark.asyncio
async def test_the_escalation_acknowledgement_implies_the_smaller_one(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Accepting "the console is my only way in" already contains "my SSH
    access closes", so demanding a second tick would teach the tick."""
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(PlatformSettings(id=1, web_ui_allowed_cidrs=["10.0.0.0/8"]))
    await db_session.commit()

    r = await client.put(
        SETTINGS,
        headers=h,
        json={
            "ssh_lockdown": True,
            "ssh_allowed_source_networks": ["10.0.0.0/8"],
            "acknowledge_console_only": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ssh_lockdown"] is True


@pytest.mark.asyncio
async def test_the_acknowledgement_is_never_written_as_a_column(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(PlatformSettings(id=1, web_ui_allowed_cidrs=["10.0.0.0/8"]))
    await db_session.commit()

    r = await client.put(
        SETTINGS,
        headers=h,
        json={
            "ssh_lockdown": True,
            "ssh_allowed_source_networks": ["10.0.0.0/8"],
            "acknowledge_console_only": True,
        },
    )
    assert r.status_code == 200, r.text
    assert "acknowledge_console_only" not in r.json()
    row = await db_session.get(PlatformSettings, 1)
    assert not hasattr(row, "acknowledge_console_only")


@pytest.mark.asyncio
async def test_no_escalation_while_the_web_ui_still_admits_you(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The per-door guard still fires — one door is closing — but the
    escalation must not, and the smaller tick must still be enough."""
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(PlatformSettings(id=1, web_ui_allowed_cidrs=["203.0.113.0/24"]))
    await db_session.commit()

    r = await client.put(
        SETTINGS,
        headers=h,
        json={"ssh_lockdown": True, "ssh_allowed_source_networks": ["10.0.0.0/8"]},
    )
    assert r.status_code == 422, r.text
    assert "last remote way in" not in r.text
    assert "not inside the allowed networks" in r.text
    # …and it names the door that survives rather than hedging about it.
    assert "203.0.113.0/24" in r.text

    forced = await client.put(
        SETTINGS,
        headers=h,
        json={
            "ssh_lockdown": True,
            "ssh_allowed_source_networks": ["10.0.0.0/8"],
            "ssh_lockdown_force": True,
        },
    )
    assert forced.status_code == 200, forced.text


@pytest.mark.asyncio
async def test_an_unrelated_ssh_edit_while_already_locked_out_does_not_escalate(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Gated on the transition, like the guard it sits above. An operator
    already in this state who adds an authorized key has not made it worse,
    and warning them again is how a tick stops being read."""
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(
        PlatformSettings(
            id=1,
            web_ui_allowed_cidrs=["10.0.0.0/8"],
            ssh_allowed_source_networks=["10.0.0.0/8"],
            ssh_lockdown=True,
        )
    )
    await db_session.commit()

    r = await client.put(
        SETTINGS,
        headers=h,
        json={"ssh_authorized_keys": [{"name": "k", "public_key": _PUBKEY}]},
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_turning_lockdown_off_never_escalates(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The recovery path. Refusing it because the caller is outside the
    scope they are removing would be exactly backwards."""
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(
        PlatformSettings(
            id=1,
            web_ui_allowed_cidrs=["10.0.0.0/8"],
            ssh_allowed_source_networks=["10.0.0.0/8"],
            ssh_lockdown=True,
        )
    )
    await db_session.commit()

    r = await client.put(SETTINGS, headers=h, json={"ssh_lockdown": False})
    assert r.status_code == 200, r.text
    assert r.json()["ssh_lockdown"] is False


# ── the Web UI write path ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_ui_restriction_that_closes_the_last_door_is_escalated(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(
        PlatformSettings(id=1, ssh_allowed_source_networks=["10.0.0.0/8"], ssh_lockdown=True)
    )
    await db_session.commit()

    r = await client.put(f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["10.0.0.0/8"]})
    assert r.status_code == 422, r.text
    assert "last remote way in" in r.json()["detail"]
    cfg = await db_session.get(PlatformSettings, 1)
    await db_session.refresh(cfg)
    assert not cfg.web_ui_allowed_cidrs


@pytest.mark.asyncio
async def test_web_ui_override_lockout_does_not_satisfy_the_escalation(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(
        PlatformSettings(id=1, ssh_allowed_source_networks=["10.0.0.0/8"], ssh_lockdown=True)
    )
    await db_session.commit()

    r = await client.put(
        f"{FW}/web-ui-access",
        headers=h,
        json={"allowed_cidrs": ["10.0.0.0/8"], "override_lockout": True},
    )
    assert r.status_code == 422, r.text
    assert "last remote way in" in r.json()["detail"]


@pytest.mark.asyncio
async def test_web_ui_escalation_acknowledgement_is_enough_on_its_own(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(
        PlatformSettings(id=1, ssh_allowed_source_networks=["10.0.0.0/8"], ssh_lockdown=True)
    )
    await db_session.commit()

    r = await client.put(
        f"{FW}/web-ui-access",
        headers=h,
        json={"allowed_cidrs": ["10.0.0.0/8"], "acknowledge_console_only": True},
    )
    assert r.status_code == 200, r.text
    cfg = await db_session.get(PlatformSettings, 1)
    await db_session.refresh(cfg)
    assert cfg.web_ui_allowed_cidrs == ["10.0.0.0/8"]


@pytest.mark.asyncio
async def test_reopening_the_web_ui_never_escalates(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(
        PlatformSettings(
            id=1,
            web_ui_allowed_cidrs=["10.0.0.0/8"],
            ssh_allowed_source_networks=["10.0.0.0/8"],
            ssh_lockdown=True,
        )
    )
    await db_session.commit()

    r = await client.put(f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": []})
    assert r.status_code == 200, r.text
    assert r.json()["open"] is True


# ── the address the guard judges ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_web_ui_guard_judges_the_trusted_address(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """It used to read ``client_ip``, which ``core/request_meta`` documents
    as spoofable and unsuitable for a source-IP allowlist gate. It is also
    simply the WRONG address behind a reverse proxy: uvicorn's
    ``--forwarded-allow-ips *`` resolves the browser's own IP out of
    X-Forwarded-For while nftables judges the packet source, which is the
    proxy — so the guard would clear an operator about to be locked out.

    Discriminating because the test transport's peer is 127.0.0.1: judging
    the peer would call this covered, judging ``X-Real-IP`` does not.
    """
    h = await _admin(db_session, ip="203.0.113.9")
    await db_session.commit()

    r = await client.put(f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["127.0.0.0/8"]})
    assert r.status_code == 422, r.text
    assert "203.0.113.9" in r.json()["detail"]

    # …and the other direction: a list covering the trusted address passes
    # even though it excludes the peer.
    ok = await client.put(
        f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["203.0.113.0/24"]}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["caller_ip"] == "203.0.113.9"
    assert ok.json()["caller_covered"] is True


# ── the read surface both screens share ─────────────────────────────


@pytest.mark.asyncio
async def test_remote_access_reports_both_doors(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """One shape read by BOTH lockout-sensitive screens, so the warning an
    operator reads and the refusal they hit cannot disagree."""
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(
        PlatformSettings(
            id=1,
            web_ui_allowed_cidrs=["203.0.113.0/24"],
            ssh_allowed_source_networks=["10.0.0.0/8"],
            ssh_lockdown=True,
        )
    )
    await db_session.commit()

    j = (await client.get("/api/v1/appliance/remote-access", headers=h)).json()
    assert j["caller_ip"] == "203.0.113.9"
    assert j["web_ui"] == {
        "name": WEB_UI,
        "restricted": True,
        "allowed_cidrs": ["203.0.113.0/24"],
        "admits": True,
    }
    assert j["ssh"]["restricted"] is True and j["ssh"]["admits"] is False
    assert j["console_only"] is False


@pytest.mark.asyncio
async def test_remote_access_is_reachable_with_the_firewall_module_off(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """It lives on the always-mounted ``/appliance`` hub rather than under
    ``/appliance/firewall``, because the SSH screen must be able to ask this
    question when the ``appliance.firewall`` module is off."""
    h = await _admin(db_session)
    db_session.add(FeatureModule(id="appliance.firewall", enabled=False))
    await db_session.commit()
    invalidate_cache()

    # The gate really is off…
    blocked = await client.get(f"{FW}/web-ui-access", headers=h)
    assert blocked.status_code == 404, blocked.text
    # …and this still answers.
    r = await client.get("/api/v1/appliance/remote-access", headers=h)
    assert r.status_code == 200, r.text


# ── the marker both UIs route on ────────────────────────────────────


@pytest.mark.asyncio
async def test_each_refusal_names_its_own_acknowledgement_and_no_other(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Both surfaces route a 422 by the acknowledgement FIELD it names, not
    by its prose — a field name is the API contract and the sentence around
    it is not. That only works if the markers are mutually exclusive, so it
    is pinned here rather than left as a property of three English sentences
    edited at different times.
    """
    h = await _admin(db_session, ip="203.0.113.9")
    db_session.add(PlatformSettings(id=1))
    await db_session.commit()

    # Web UI, one door closing (SSH unrestricted).
    per_door = await client.put(
        f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["10.0.0.0/8"]}
    )
    assert per_door.status_code == 422
    detail = per_door.json()["detail"]
    assert "override_lockout" in detail
    assert "acknowledge_console_only" not in detail

    # SSH, one door closing (Web UI unrestricted).
    ssh_per_door = await client.put(
        SETTINGS,
        headers=h,
        json={"ssh_lockdown": True, "ssh_allowed_source_networks": ["10.0.0.0/8"]},
    )
    assert ssh_per_door.status_code == 422
    ssh_detail = ssh_per_door.json()["detail"]
    assert "ssh_lockdown_force" in ssh_detail
    assert "acknowledge_console_only" not in ssh_detail

    # …and the escalation, from BOTH paths, is the same text.
    row = await db_session.get(PlatformSettings, 1)
    row.web_ui_allowed_cidrs = ["10.0.0.0/8"]
    await db_session.commit()
    escalated = await client.put(
        SETTINGS,
        headers=h,
        json={"ssh_lockdown": True, "ssh_allowed_source_networks": ["10.0.0.0/8"]},
    )
    assert escalated.status_code == 422
    esc_detail = escalated.json()["detail"]
    assert "acknowledge_console_only" in esc_detail
    assert "override_lockout" not in esc_detail
    assert "ssh_lockdown_force" not in esc_detail

    row = await db_session.get(PlatformSettings, 1)
    row.web_ui_allowed_cidrs = []
    row.ssh_allowed_source_networks = ["10.0.0.0/8"]
    row.ssh_lockdown = True
    await db_session.commit()
    from_firewall = await client.put(
        f"{FW}/web-ui-access", headers=h, json={"allowed_cidrs": ["10.0.0.0/8"]}
    )
    assert from_firewall.status_code == 422
    # One state of the appliance, so one sentence about it — an operator who
    # meets it from either screen is being told about the same two lists.
    assert from_firewall.json()["detail"] == esc_detail


# ── the copilot's view ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_copilot_tool_reports_both_lists_together(
    db_session: AsyncSession,
) -> None:
    """Neither existing tool could see the composition: ``find_web_ui_access``
    is tagged ``module="appliance.firewall"`` and ``find_ssh_settings`` is
    not, so with that module off the copilot could see one door and not the
    other — the blindness this issue is about, in the chat surface."""
    from app.services.ai.tools.ssh import (  # noqa: PLC0415
        FindRemoteAccessDoorsArgs,
        find_remote_access_doors,
    )

    db_session.add(
        PlatformSettings(
            id=1,
            web_ui_allowed_cidrs=["10.0.0.0/8"],
            ssh_allowed_source_networks=["192.168.0.0/24"],
            ssh_lockdown=True,
        )
    )
    await db_session.flush()

    out = await find_remote_access_doors(db_session, None, FindRemoteAccessDoorsArgs())
    assert out["both_restricted"] is True
    assert out["web_ui"]["allowed_cidrs"] == ["10.0.0.0/8"]
    assert out["ssh"]["allowed_cidrs"] == ["192.168.0.0/24"]
    assert "console" in out["summary"]


@pytest.mark.asyncio
async def test_the_copilot_tool_reports_a_configured_but_unenforced_ssh_list(
    db_session: AsyncSession,
) -> None:
    """``ssh_lockdown`` off means the list restricts nothing, so reporting it
    as a restriction would tell the operator a door is shut that is open."""
    from app.services.ai.tools.ssh import (  # noqa: PLC0415
        FindRemoteAccessDoorsArgs,
        find_remote_access_doors,
    )

    db_session.add(
        PlatformSettings(
            id=1,
            web_ui_allowed_cidrs=["10.0.0.0/8"],
            ssh_allowed_source_networks=["192.168.0.0/24"],
            ssh_lockdown=False,
        )
    )
    await db_session.flush()

    out = await find_remote_access_doors(db_session, None, FindRemoteAccessDoorsArgs())
    assert out["both_restricted"] is False
    assert out["ssh"]["restricted"] is False
    assert out["ssh"]["allowed_cidrs"] == []
    assert "recoverable" in out["summary"]
