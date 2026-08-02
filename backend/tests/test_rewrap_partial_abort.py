"""#781 — a rewrap that dies part-way through must report what it did.

``rewrap_secrets`` walks 47 ``LargeBinary`` columns and 18 JSONB fields,
each in its own transaction, committing as it goes. If the walk dies at
column 30, the credential store is genuinely half-migrated: 29 columns
now decrypt under the DESTINATION key, the rest still only under the
SOURCE key.

The old shape lost exactly that information. ``rewrap_secrets`` raised,
and ``restore.py`` answered with a fresh ``RewrapOutcome()`` — so the
audit row and the API response both said ``rewrapped_rows=0``. An
operator reading "0 rows rewrapped" concludes nothing happened and that
re-running is safe/unnecessary, when in fact the install is in the one
state that needs immediate attention.

These tests pin the honest contract: the partial counters survive, the
abort is flagged, and the reason names how far it got.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest

from app.services.backup.rewrap import (
    ENCRYPTED_COLUMNS,
    JSONB_ENCRYPTED_FIELDS,
    RewrapOutcome,
    _fernet_from_keys,
    rewrap_secrets,
)


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


class _FakeConn:
    """Minimal asyncpg-connection stand-in.

    Serves ``rows_per_table`` rows for the first ``die_after`` fetches,
    then raises. Nothing here talks to Postgres — the point is the
    control flow around the walk, not SQL.
    """

    def __init__(self, *, die_after: int, blob: bytes | None = None) -> None:
        self.die_after = die_after
        self.fetches = 0
        self.closed = False
        self._blob = blob

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches += 1
        if self.fetches > self.die_after:
            raise ConnectionResetError("connection to server was lost")
        if self._blob is None:
            return []
        # ``_rewrap_column`` indexes rows by COLUMN NAME (``row[pk_col]``),
        # which real asyncpg Records support and a plain tuple does not.
        # Parse the names back out of the SELECT so one fake serves every
        # column in the walk.
        select_part = query.split(" FROM ", 1)[0].removeprefix("SELECT ")
        pk_col, enc_col = (c.strip().strip('"') for c in select_part.split(",", 1))
        return [{pk_col: 1, enc_col: self._blob}]

    async def execute(self, query: str, *args: Any) -> str:
        return "UPDATE 1"

    def transaction(self) -> Any:
        conn = self

        class _Tx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        del conn
        return _Tx()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_abort_midwalk_keeps_the_counters_and_flags_itself(monkeypatch) -> None:
    src, dst = _key(), _key()
    # One real row per column so the walk has something to count first.
    # Encrypt with the SAME derivation ``rewrap_secrets`` uses — it
    # SHA-256s the secret key rather than treating it as Fernet material,
    # so a raw ``Fernet(src)`` token would come back undecryptable and
    # land in ``failed_rows`` instead of ``rewrapped_rows``.
    fake = _FakeConn(die_after=3, blob=_fernet_from_keys(src, "").encrypt(b"a-credential"))

    async def _connect(**_kw: Any) -> _FakeConn:
        return fake

    monkeypatch.setattr("app.services.backup.rewrap.asyncpg.connect", _connect)

    outcome = await rewrap_secrets(
        db_url="postgresql+asyncpg://u:p@h/db",
        source_secret_key=src,
        source_credential_key="",
        dest_secret_key=dst,
        dest_credential_key="",
    )

    assert outcome.aborted is True
    # The walk got somewhere — this is the whole point. The old code
    # returned a fresh RewrapOutcome() here, i.e. all zeros.
    assert outcome.columns_visited > 0
    assert outcome.rewrapped_rows > 0
    # ...and stopped well short of the full set.
    total = len(ENCRYPTED_COLUMNS) + len(JSONB_ENCRYPTED_FIELDS)
    assert outcome.columns_visited < total
    # The failure entry says how far it got, so the operator can judge
    # the blast radius without re-deriving it.
    assert outcome.failures
    reason = outcome.failures[-1]
    assert "rewrap-aborted" in reason["reason"]
    assert reason["columns_total"] == total
    # The connection is still released on the abort path.
    assert fake.closed is True


@pytest.mark.asyncio
async def test_a_connect_failure_reports_zero_because_zero_is_true(monkeypatch) -> None:
    """The all-zeros answer is correct when the walk never started.

    Distinguishing this from the mid-walk case is the reason the fix is
    a flag plus real counters rather than a flag alone.
    """

    async def _connect(**_kw: Any) -> Any:
        raise OSError("no route to host")

    monkeypatch.setattr("app.services.backup.rewrap.asyncpg.connect", _connect)

    outcome = await rewrap_secrets(
        db_url="postgresql+asyncpg://u:p@h/db",
        source_secret_key=_key(),
        source_credential_key="",
        dest_secret_key=_key(),
        dest_credential_key="",
    )

    assert outcome.aborted is True
    assert outcome.columns_visited == 0
    assert outcome.rewrapped_rows == 0
    assert "connect failed" in outcome.failures[-1]["reason"]


@pytest.mark.asyncio
async def test_a_clean_walk_is_not_flagged_as_aborted(monkeypatch) -> None:
    """Guard against over-correcting: the flag must mean something."""
    fake = _FakeConn(die_after=10_000, blob=None)

    async def _connect(**_kw: Any) -> _FakeConn:
        return fake

    monkeypatch.setattr("app.services.backup.rewrap.asyncpg.connect", _connect)

    outcome = await rewrap_secrets(
        db_url="postgresql+asyncpg://u:p@h/db",
        source_secret_key=_key(),
        source_credential_key="",
        dest_secret_key=_key(),
        dest_credential_key="",
    )

    assert outcome.aborted is False
    assert outcome.columns_visited == len(ENCRYPTED_COLUMNS) + len(JSONB_ENCRYPTED_FIELDS)
    assert outcome.failures == []


def test_same_install_short_circuit_is_not_an_abort() -> None:
    """A same-install restore returns zeros legitimately; it must not
    look like a failure to whatever reads the outcome."""
    o = RewrapOutcome(same_install=True)
    assert o.aborted is False
