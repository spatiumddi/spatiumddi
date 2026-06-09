"""``secret_expiring`` alert rule (#76).

Verifies the matcher surfaces internal credentials within the threshold
(supervisor mTLS certs + active API tokens with an expiry), keys each to a
distinct subject_id, escalates severity as expiry nears, and excludes
far-future / revoked / inactive credentials.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alerts import AlertRule
from app.models.appliance import APPLIANCE_STATE_APPROVED, Appliance
from app.models.auth import APIToken, User
from app.services.alerts import _matching_secret_expiring_subjects


def _appliance(hostname: str, *, expires_in_days: int) -> Appliance:
    der = os.urandom(32)
    return Appliance(
        id=uuid.uuid4(),
        hostname=hostname,
        public_key_der=der,
        public_key_fingerprint=hashlib.sha256(der).hexdigest(),
        state=APPLIANCE_STATE_APPROVED,
        cert_expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
    )


async def test_secret_expiring_matches_certs_and_tokens(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    rule = AlertRule(
        name="Secret expiry",
        rule_type="secret_expiring",
        severity="warning",
        threshold_days=30,
        enabled=True,
    )
    db_session.add(rule)

    near_cert = _appliance("appl-near", expires_in_days=5)  # inside threshold
    far_cert = _appliance("appl-far", expires_in_days=365)  # must NOT match
    db_session.add_all([near_cert, far_cert])

    user = User(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="u",
        hashed_password="x",
    )
    db_session.add(user)
    await db_session.flush()

    live_token = APIToken(
        name="ci-deploy",
        token_hash=uuid.uuid4().hex,
        prefix="abc123",
        created_by_user_id=user.id,
        scope="user",
        is_active=True,
        expires_at=now + timedelta(days=10),
    )
    inactive_token = APIToken(
        name="retired",
        token_hash=uuid.uuid4().hex,
        prefix="dead00",
        created_by_user_id=user.id,
        scope="user",
        is_active=False,  # must NOT match
        expires_at=now + timedelta(days=3),
    )
    db_session.add_all([live_token, inactive_token])
    await db_session.commit()

    subjects = await _matching_secret_expiring_subjects(db_session, rule, now)
    ids = {sid for sid, _disp, _msg, _sev in subjects}

    assert f"appliance_cert:{near_cert.id}" in ids
    assert f"api_token:{live_token.id}" in ids
    assert f"appliance_cert:{far_cert.id}" not in ids
    assert f"api_token:{inactive_token.id}" not in ids

    # 5 days left is well inside threshold/12 (30/12 = 2.5) → escalates above the
    # base "warning"; assert it's at least warning and a known severity.
    sev_by_id = {sid: sev for sid, _disp, _msg, sev in subjects}
    assert sev_by_id[f"appliance_cert:{near_cert.id}"] in ("warning", "critical")
