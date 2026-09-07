"""Write-only + immutable backup destinations (issue #989 items 1, 2, 4).

The gap this closes is that the credential which writes an archive could
also delete it, and the retention sweep ran with that credential every
night. So the tests that matter are the ones proving each of the four
behaviours actually changes, plus the two places where the *old* code
would now say something false:

* an empty listing from a destination we are not permitted to list must
  not be reported as "the destination holds no archives" — that is a
  confident claim about a target that is probably fine;
* a refused delete on an Object Lock bucket must not be logged as a
  failure once per archive per night.

``https_put`` is covered here rather than in its own file because its
whole design is a consequence of item 1: it is write-only by
construction, so it is the case that exercises the forcing logic.
"""

from __future__ import annotations

import errno
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1.backup.targets import (
    _archive_etag,
    _assert_retention_is_reachable,
    _if_none_match_matches,
    _resolve_write_only,
)
from app.services.backup.targets import (
    DESTINATIONS,
    RetentionLockedError,
    UnsupportedOperationError,
    get_destination,
    is_retention_locked,
)
from app.services.backup.targets.base import BackupDestinationError, DestinationConfigError
from app.services.backup.targets.https_put import (
    HttpsPutDestination,
    _parse_extra_headers,
    _target_url,
)
from app.services.backup.targets.s3 import _lock_headers, _lock_mode

# ── item 1: the locked-object classification ──────────────────────────


def test_retention_locked_is_a_typed_error_not_a_string_match():
    """The prune has to tell "refused because locked" from "refused
    because broken", and the message it would otherwise match on comes
    from the storage SDK — not ours to depend on.
    """
    assert is_retention_locked(RetentionLockedError("locked until 2027"))
    assert not is_retention_locked(BackupDestinationError("connection reset"))
    assert not is_retention_locked(OSError(errno.EACCES, "denied"))


def test_retention_locked_is_still_a_destination_error():
    # The runner catches BackupDestinationError broadly; a subclass that
    # escaped that would turn a locked object into a crashed backup run.
    assert isinstance(RetentionLockedError("x"), BackupDestinationError)
    assert isinstance(UnsupportedOperationError("x"), BackupDestinationError)


# ── item 1: S3 Object Lock ────────────────────────────────────────────


@pytest.mark.parametrize("value", ["", "none", "off", "disabled", None])
def test_lock_mode_off_spellings(value):
    assert _lock_mode({"object_lock_mode": value}) is None


def test_lock_mode_maps_to_the_api_spelling():
    assert _lock_mode({"object_lock_mode": "compliance"}) == "COMPLIANCE"
    assert _lock_mode({"object_lock_mode": "Governance"}) == "GOVERNANCE"


def test_lock_mode_refuses_an_unknown_value():
    # Silently treating a typo as "off" would leave an operator believing
    # their archives are immutable when nothing is locking them.
    with pytest.raises(DestinationConfigError):
        _lock_mode({"object_lock_mode": "complianse"})


def test_lock_headers_are_empty_when_locking_is_off():
    assert _lock_headers({"object_lock_mode": "none"}) == {}


def test_lock_headers_carry_mode_and_a_future_retain_date():
    headers = _lock_headers({"object_lock_mode": "compliance", "object_lock_days": "30"})
    assert headers["ObjectLockMode"] == "COMPLIANCE"
    delta = headers["ObjectLockRetainUntilDate"] - datetime.now(UTC)
    assert timedelta(days=29) < delta <= timedelta(days=30)


def test_s3_requires_a_retention_period_with_a_lock_mode():
    """A lock with no period is a no-op that reads as protection."""
    driver = get_destination("s3")
    base = {
        "bucket": "b",
        "region": "us-east-1",
        "access_key_id": "k",
        "secret_access_key": "s",
    }
    driver.validate_config(base)  # no locking: fine
    with pytest.raises(DestinationConfigError):
        driver.validate_config({**base, "object_lock_mode": "compliance"})
    with pytest.raises(DestinationConfigError):
        driver.validate_config({**base, "object_lock_mode": "compliance", "object_lock_days": "0"})
    driver.validate_config({**base, "object_lock_mode": "compliance", "object_lock_days": "30"})


class _FakeS3Client:
    """Minimal stand-in for the boto3 S3 client.

    Records every call so the tests below can assert on the *request*
    that would go on the wire, rather than grepping the source — which
    is what the first cut of these tests did, and it matched the comment
    explaining the behaviour instead of the behaviour.
    """

    def __init__(self, *, head: dict | None = None, delete_raises: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._head = head if head is not None else {"ContentLength": 16}
        self._delete_raises = delete_raises

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))
        return {}

    def head_object(self, **kw):
        self.calls.append(("head_object", kw))
        return self._head

    def delete_object(self, **kw):
        self.calls.append(("delete_object", kw))
        if self._delete_raises is not None:
            raise self._delete_raises
        return {}

    def kwargs_for(self, op: str) -> dict:
        return next(kw for name, kw in self.calls if name == op)

    def called(self, op: str) -> bool:
        return any(name == op for name, _ in self.calls)


_S3_BASE = {
    "bucket": "b",
    "region": "us-east-1",
    "access_key_id": "k",
    "secret_access_key": "s",
    "object_lock_mode": "compliance",
    "object_lock_days": "30",
}


def _patch_client(monkeypatch, fake):
    from app.services.backup.targets import s3 as s3_mod

    monkeypatch.setattr(s3_mod, "_client", lambda config: fake)
    return s3_mod


@pytest.mark.asyncio
async def test_the_s3_write_applies_the_configured_lock(monkeypatch):
    fake = _FakeS3Client()
    _patch_client(monkeypatch, fake)
    await get_destination("s3").write(
        config=_S3_BASE, filename="spatiumddi-backup-1.zip", archive_bytes=b"x"
    )
    kw = fake.kwargs_for("put_object")
    assert kw["ObjectLockMode"] == "COMPLIANCE"
    assert kw["ObjectLockRetainUntilDate"] > datetime.now(UTC)


@pytest.mark.asyncio
async def test_the_s3_probe_is_written_without_lock_headers(monkeypatch):
    """A probe under a 30-day compliance lock is undeletable litter,
    created every time somebody clicks Test.
    """
    fake = _FakeS3Client()
    _patch_client(monkeypatch, fake)
    result = await get_destination("s3").test_connection(config=_S3_BASE)
    assert result["ok"] is True
    kw = fake.kwargs_for("put_object")
    assert "ObjectLockMode" not in kw
    assert "ObjectLockRetainUntilDate" not in kw


@pytest.mark.asyncio
async def test_the_s3_probe_passes_when_the_key_cannot_delete(monkeypatch):
    """The recommended shape is a key with no ``DeleteObject``. Failing
    the probe at the delete step is what trains operators to widen the
    key — i.e. the surface arguing against its own best practice.
    """
    from botocore.exceptions import ClientError

    denied = ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
    fake = _FakeS3Client(delete_raises=denied)
    _patch_client(monkeypatch, fake)
    result = await get_destination("s3").test_connection(config=_S3_BASE)
    assert result["ok"] is True
    assert result["probe_retained"] is True
    assert "write-only" in result["detail"]


@pytest.mark.asyncio
async def test_s3_delete_refuses_an_object_still_under_retention(monkeypatch):
    fake = _FakeS3Client(
        head={
            "ContentLength": 16,
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": datetime.now(UTC) + timedelta(days=5),
        }
    )
    _patch_client(monkeypatch, fake)
    with pytest.raises(RetentionLockedError):
        await get_destination("s3").delete(config=_S3_BASE, filename="spatiumddi-backup-1.zip")
    # And it never even tried — asking first is what lets the retention
    # sweep stay quiet about a lock instead of logging a failure.
    assert not fake.called("delete_object")


@pytest.mark.asyncio
async def test_s3_delete_refuses_an_object_under_legal_hold(monkeypatch):
    fake = _FakeS3Client(head={"ContentLength": 16, "ObjectLockLegalHoldStatus": "ON"})
    _patch_client(monkeypatch, fake)
    with pytest.raises(RetentionLockedError):
        await get_destination("s3").delete(config=_S3_BASE, filename="spatiumddi-backup-1.zip")


@pytest.mark.asyncio
async def test_s3_delete_proceeds_once_the_retention_has_expired(monkeypatch):
    fake = _FakeS3Client(
        head={
            "ContentLength": 16,
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": datetime.now(UTC) - timedelta(days=1),
        }
    )
    _patch_client(monkeypatch, fake)
    await get_destination("s3").delete(config=_S3_BASE, filename="spatiumddi-backup-1.zip")
    assert fake.called("delete_object")


@pytest.mark.asyncio
async def test_s3_delete_still_works_when_head_is_not_permitted(monkeypatch):
    """A key with DeleteObject but no head grant must not lose the
    ability to prune — the head is an optimisation, not a gate.
    """
    from botocore.exceptions import ClientError

    class _NoHead(_FakeS3Client):
        def head_object(self, **kw):
            self.calls.append(("head_object", kw))
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")

    fake = _NoHead()
    _patch_client(monkeypatch, fake)
    await get_destination("s3").delete(config=_S3_BASE, filename="spatiumddi-backup-1.zip")
    assert fake.called("delete_object")


# ── item 1: forcing + refusals ────────────────────────────────────────


def test_write_only_is_forced_for_a_kind_that_cannot_delete():
    https = get_destination("https_put")
    assert https.inherently_write_only is True
    assert _resolve_write_only(https, False) is True
    assert _resolve_write_only(https, True) is True


def test_write_only_is_the_operator_s_choice_for_a_normal_kind():
    s3 = get_destination("s3")
    assert s3.inherently_write_only is False
    assert _resolve_write_only(s3, False) is False
    assert _resolve_write_only(s3, True) is True


def test_retention_on_a_write_only_target_is_refused():
    """Not cosmetic: accepting it would put a number on screen that
    silently does nothing every night — the exact failure this item
    exists to remove.
    """
    from fastapi import HTTPException

    s3 = get_destination("s3")
    with pytest.raises(HTTPException) as exc:
        _assert_retention_is_reachable(s3, write_only=True, keep_n=7, keep_days=None)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        _assert_retention_is_reachable(s3, write_only=True, keep_n=None, keep_days=30)


def test_retention_is_fine_without_write_only_and_absent_with_it():
    s3 = get_destination("s3")
    _assert_retention_is_reachable(s3, write_only=False, keep_n=7, keep_days=None)
    _assert_retention_is_reachable(s3, write_only=True, keep_n=None, keep_days=None)


def test_the_refusal_names_the_kind_when_the_kind_is_the_reason():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _assert_retention_is_reachable(
            get_destination("https_put"), write_only=True, keep_n=3, keep_days=None
        )
    assert "cannot delete" in str(exc.value.detail)


# ── item 2: https_put ─────────────────────────────────────────────────


def test_https_put_is_registered():
    assert "https_put" in DESTINATIONS


@pytest.mark.asyncio
async def test_https_put_cannot_read_or_delete():
    driver = HttpsPutDestination()
    config = {"url": "https://nexus.example/repository/backups"}
    with pytest.raises(UnsupportedOperationError):
        await driver.download(config=config, filename="a.zip")
    with pytest.raises(UnsupportedOperationError):
        await driver.delete(config=config, filename="a.zip")


@pytest.mark.asyncio
async def test_https_put_lists_empty_rather_than_raising():
    """An empty listing is what the retention sweep needs to mean
    "nothing to prune". Raising would fail every scheduled run.
    """
    assert await HttpsPutDestination().list_archives(config={"url": "https://x/y"}) == []


def test_target_url_substitutes_the_filename_placeholder():
    cfg = {"url": "https://art.example/generic/{filename}"}
    assert _target_url(cfg, "spatiumddi-backup-1.zip") == (
        "https://art.example/generic/spatiumddi-backup-1.zip"
    )


def test_target_url_leaves_a_presigned_url_alone():
    """Appending a path segment to a presigned URL invalidates the
    signature, and the far end's error says nothing useful about why.
    """
    presigned = "https://bucket.s3.amazonaws.com/key?X-Amz-Signature=abc&X-Amz-Expires=900"
    assert _target_url({"url": presigned}, "spatiumddi-backup-1.zip") == presigned


def test_target_url_appends_when_there_is_no_placeholder_or_query():
    cfg = {"url": "https://nexus.example/repository/backups/"}
    assert _target_url(cfg, "spatiumddi-backup-1.zip") == (
        "https://nexus.example/repository/backups/spatiumddi-backup-1.zip"
    )


def test_target_url_strips_a_directory_traversal_from_the_filename():
    cfg = {"url": "https://nexus.example/repository/backups"}
    assert _target_url(cfg, "../../evil.zip").endswith("/evil.zip")


def test_extra_headers_parse_and_refuse_malformed_lines():
    assert _parse_extra_headers({"extra_headers": "X-A: 1\nX-B: 2"}) == {"X-A": "1", "X-B": "2"}
    assert _parse_extra_headers({"extra_headers": ""}) == {}
    with pytest.raises(DestinationConfigError):
        _parse_extra_headers({"extra_headers": "no-colon-here"})


def test_https_put_validation_requires_a_credential_for_each_auth_mode():
    driver = HttpsPutDestination()
    driver.validate_config({"url": "https://x.example/y"})  # auth defaults to none
    with pytest.raises(DestinationConfigError):
        driver.validate_config({"url": "https://x.example/y", "auth": "bearer"})
    with pytest.raises(DestinationConfigError):
        driver.validate_config(
            {"url": "https://x.example/y", "auth": "basic", "credential": "p"}
        )  # username missing
    with pytest.raises(DestinationConfigError):
        driver.validate_config(
            {"url": "https://x.example/y", "auth": "header", "credential": "p"}
        )  # header_name missing
    with pytest.raises(DestinationConfigError):
        driver.validate_config({"url": "https://x.example/y", "auth": "nonsense"})


def test_https_put_refuses_a_non_http_url_and_a_bad_method():
    driver = HttpsPutDestination()
    with pytest.raises(DestinationConfigError):
        driver.validate_config({"url": "ftp://x.example/y"})
    with pytest.raises(DestinationConfigError):
        driver.validate_config({"url": "https://x.example/y", "method": "DELETE"})


@pytest.mark.asyncio
async def test_https_put_ssrf_guard_blocks_a_loopback_url():
    """This destination carries the whole database off-box under an
    operator-supplied URL, so the guard blocks rather than warns.
    """
    with pytest.raises(DestinationConfigError):
        await HttpsPutDestination().validate_config_network({"url": "http://127.0.0.1/receive"})


@pytest.mark.asyncio
async def test_https_put_ssrf_guard_blocks_the_cloud_metadata_address():
    with pytest.raises(DestinationConfigError):
        await HttpsPutDestination().validate_config_network(
            {"url": "http://169.254.169.254/latest/meta-data/"}
        )


def test_https_put_does_not_follow_redirects():
    """A followed redirect re-sends the archive AND its credential to an
    address the SSRF guard never saw.
    """
    client = HttpsPutDestination()._client({"url": "https://x.example/y"})
    assert client.follow_redirects is False


def test_https_put_carries_the_write_only_caveat_as_a_notice():
    notices = [f for f in get_destination("https_put").config_fields if f.type == "notice"]
    assert len(notices) == 1
    assert "write-only" in (notices[0].label + (notices[0].description or "")).lower()


# ── item 4: conditional GET ───────────────────────────────────────────


def test_archive_etag_is_quoted():
    # A bare token is not a valid entity-tag; some clients drop it.
    assert _archive_etag("spatiumddi-backup-1.zip") == '"spatiumddi-backup-1.zip"'


def _req(header: str | None):
    return SimpleNamespace(headers={} if header is None else {"if-none-match": header})


def test_if_none_match_matches_exact_star_and_list():
    etag = _archive_etag("a.zip")
    assert _if_none_match_matches(_req('"a.zip"'), etag)
    assert _if_none_match_matches(_req("*"), etag)
    assert _if_none_match_matches(_req('"b.zip", "a.zip"'), etag)


def test_if_none_match_handles_the_weak_prefix():
    # RFC 9110 §13.1.2: a GET compares with the weak function, so a
    # proxy that weakened the tag must still get its 304.
    assert _if_none_match_matches(_req('W/"a.zip"'), _archive_etag("a.zip"))


def test_if_none_match_does_not_match_a_different_archive():
    assert not _if_none_match_matches(_req('"older.zip"'), _archive_etag("a.zip"))
    assert not _if_none_match_matches(_req(None), _archive_etag("a.zip"))


# ── item 1: the two places the old code stated something false ────────


class _CountingDriver:
    """Records deletes so the prune's behaviour is observable."""

    def __init__(self, *, archives: list[str], delete_raises: Exception | None = None):
        self._archives = archives
        self._delete_raises = delete_raises
        self.deleted: list[str] = []

    async def list_archives(self, *, config):
        from app.services.backup.targets.base import ArchiveListing

        now = datetime.now(UTC)
        return [
            ArchiveListing(filename=n, size_bytes=1, created_at=now - timedelta(days=i))
            for i, n in enumerate(self._archives)
        ]

    async def delete(self, *, config, filename):
        if self._delete_raises is not None:
            raise self._delete_raises
        self.deleted.append(filename)


def _target(**kw):
    from app.models.backup import BackupTarget

    defaults = dict(name="t", kind="local_volume", config={}, passphrase_encrypted=b"x")
    return BackupTarget(**{**defaults, **kw})


@pytest.mark.asyncio
async def test_a_write_only_target_never_prunes(monkeypatch):
    """The prune is what would need a delete credential — which is the
    exact thing this flag exists to make unnecessary.
    """
    from app.services.backup import runner as runner_mod

    driver = _CountingDriver(archives=["a.zip", "b.zip", "c.zip"])
    monkeypatch.setattr(runner_mod, "get_destination", lambda kind: driver)
    target = _target(write_only=True, retention_keep_last_n=1)
    deleted = await runner_mod._retention_sweep(None, target=target, config={})
    assert deleted == 0
    assert driver.deleted == [], "a write-only target must not delete anything"


@pytest.mark.asyncio
async def test_a_normal_target_still_prunes(monkeypatch):
    # The negative control: without it the test above passes on a
    # sweep that is broken for every target.
    from app.services.backup import runner as runner_mod

    driver = _CountingDriver(archives=["a.zip", "b.zip", "c.zip"])
    monkeypatch.setattr(runner_mod, "get_destination", lambda kind: driver)
    target = _target(write_only=False, retention_keep_last_n=1)
    deleted = await runner_mod._retention_sweep(None, target=target, config={})
    assert deleted == 2
    assert driver.deleted == ["b.zip", "c.zip"]


@pytest.mark.asyncio
async def test_a_locked_object_is_skipped_quietly_not_warned_about(monkeypatch):
    """On an Object Lock bucket every prune attempt is refused. Logging
    that at WARNING once per archive per night makes a correctly
    configured install indistinguishable from a broken one.

    Captured through ``structlog.testing`` rather than pytest's
    ``caplog``: these are structlog events, which go out through their
    own processor pipeline and never reach the stdlib handler caplog
    hooks — so a caplog-based assertion reads empty whatever the code
    does. (Found the honest way: the *positive* half of this assertion
    failed. An absence-only test would have passed vacuously.)
    """
    from structlog.testing import capture_logs

    from app.services.backup import runner as runner_mod

    driver = _CountingDriver(
        archives=["a.zip", "b.zip", "c.zip"],
        delete_raises=RetentionLockedError("locked until 2027-01-01"),
    )
    monkeypatch.setattr(runner_mod, "get_destination", lambda kind: driver)
    target = _target(write_only=False, retention_keep_last_n=1)
    with capture_logs() as events:
        deleted = await runner_mod._retention_sweep(None, target=target, config={})
    assert deleted == 0
    names = [e.get("event") for e in events]
    assert "backup_retention_delete_failed" not in names
    assert names.count("backup_retention_object_locked") == 2
    assert all(e["log_level"] == "debug" for e in events)


@pytest.mark.asyncio
async def test_a_genuine_delete_failure_still_warns(monkeypatch):
    # Negative control for the test above: a real failure must keep
    # warning, or "quiet" would just mean "silent about everything".
    from structlog.testing import capture_logs

    from app.services.backup import runner as runner_mod

    driver = _CountingDriver(
        archives=["a.zip", "b.zip"],
        delete_raises=BackupDestinationError("connection reset"),
    )
    monkeypatch.setattr(runner_mod, "get_destination", lambda kind: driver)
    target = _target(write_only=False, retention_keep_last_n=1)
    with capture_logs() as events:
        await runner_mod._retention_sweep(None, target=target, config={})
    warned = [e for e in events if e.get("event") == "backup_retention_delete_failed"]
    assert warned and warned[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_an_empty_listing_on_a_write_only_target_is_not_a_failed_drill(monkeypatch):
    """THE important one.

    A destination we are not permitted to list returns nothing. The old
    code turned that into ``failed`` with "the destination holds no
    archives" — a confident, alerting claim about a target that is very
    likely fine. The honest verdict is that we cannot tell.
    """
    from app.services.backup import drill as drill_mod
    from app.services.backup.drill import CANNOT_DRILL, _execute

    async def _ok(**kwargs):
        return None

    monkeypatch.setattr(drill_mod, "preflight", _ok)
    monkeypatch.setattr(drill_mod, "get_destination", lambda kind: _CountingDriver(archives=[]))
    monkeypatch.setattr(drill_mod, "decrypt_config_secrets", lambda d, c: c)

    outcome = await _execute(
        _target(write_only=True), live_db_url="postgresql+asyncpg://u:p@h:5432/live"
    )
    assert outcome.state == CANNOT_DRILL
    assert outcome.state != "failed"
    assert outcome.assertions == [], "nothing was checked, so nothing should be claimed"
    assert "write-only" in (outcome.error or "")


@pytest.mark.asyncio
async def test_an_empty_listing_on_a_normal_target_is_still_a_failed_drill(monkeypatch):
    # Negative control for the branch above: on a target we CAN list, an
    # empty destination is a real finding and must keep alerting.
    from app.services.backup import drill as drill_mod
    from app.services.backup.drill import _execute

    async def _ok(**kwargs):
        return None

    monkeypatch.setattr(drill_mod, "preflight", _ok)
    monkeypatch.setattr(drill_mod, "get_destination", lambda kind: _CountingDriver(archives=[]))
    monkeypatch.setattr(drill_mod, "decrypt_config_secrets", lambda d, c: c)

    outcome = await _execute(
        _target(write_only=False), live_db_url="postgresql+asyncpg://u:p@h:5432/live"
    )
    assert outcome.state == "failed"
    assert [a.name for a in outcome.assertions] == ["archive_available"]


@pytest.mark.asyncio
async def test_an_unlistable_write_only_destination_is_cannot_drill_not_error(monkeypatch):
    from app.services.backup import drill as drill_mod
    from app.services.backup.drill import CANNOT_DRILL, _execute

    class _Unlistable(_CountingDriver):
        async def list_archives(self, *, config):
            raise BackupDestinationError("AccessDenied on ListBucket")

    async def _ok(**kwargs):
        return None

    monkeypatch.setattr(drill_mod, "preflight", _ok)
    monkeypatch.setattr(drill_mod, "get_destination", lambda kind: _Unlistable(archives=[]))
    monkeypatch.setattr(drill_mod, "decrypt_config_secrets", lambda d, c: c)

    outcome = await _execute(
        _target(write_only=True), live_db_url="postgresql+asyncpg://u:p@h:5432/live"
    )
    assert outcome.state == CANNOT_DRILL
