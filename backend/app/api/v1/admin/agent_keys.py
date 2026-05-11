"""Agent bootstrap key reveal (Phase 6 prerequisite, issue #134).

The control plane carries pre-shared keys (``DNS_AGENT_KEY`` +
``DHCP_AGENT_KEY``) that every distributed agent (DNS or DHCP) needs
to paste in on first boot to register. The keys live in
``/etc/spatiumddi/.env`` on the control-plane host today; without a
UI surface, operators have to SSH into the host and ``cat .env`` to
find them — which contradicts the "manage from the web UI" goal and
makes the role-split appliance installer's "enter your agent key"
prompt useless to anyone who didn't set up the control plane.

This endpoint exposes the keys through a password-confirm reveal
flow:

  POST /api/v1/admin/agent-keys/reveal  {password}
       -> 200 {dns_agent_key, dhcp_agent_key}
       on bad password -> 403
       on non-superadmin -> 403
       audit row emitted on both success + failure

The endpoint deliberately uses POST (not GET) so the password
travels in the request body, not a URL that might end up in nginx /
proxy logs. The reveal action is audited so superadmin-on-superadmin
abuse is at least visible in the audit log.

Applies to every deployment topology — docker-compose, k8s,
appliance — because the keys live in the api's env regardless of
how the api was deployed.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.config import settings
from app.core.security import verify_password
from app.models.audit import AuditLog

logger = structlog.get_logger(__name__)

router = APIRouter()


class RevealRequest(BaseModel):
    password: str = Field(min_length=1)


class RevealResponse(BaseModel):
    dns_agent_key: str
    dhcp_agent_key: str
    # Hint flags so the UI can distinguish "not configured" from
    # "the operator hasn't yet enabled the DNS/DHCP agent profile".
    dns_agent_configured: bool
    dhcp_agent_configured: bool


@router.post(
    "/agent-keys/reveal",
    response_model=RevealResponse,
    summary="Reveal DNS + DHCP agent bootstrap keys (superadmin + password-confirm)",
)
async def reveal_agent_keys(
    body: RevealRequest,
    current_user: CurrentUser,
    db: DB,
) -> RevealResponse:
    """Return the agent bootstrap keys after password re-verification.

    Restricted to superadmin because the keys are operationally
    equivalent to ``sudo`` over the control plane's DNS / DHCP
    surface — anyone who has them can register a rogue agent and
    inject themselves into the agent-config push.
    """
    if not current_user.is_superadmin:
        # Audit even the denied attempts — interesting signal.
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="agent_keys_reveal_denied",
                resource_type="platform",
                resource_id="agent-keys",
                resource_display="agent bootstrap keys",
                result="forbidden",
                new_value={"reason": "non_superadmin"},
            )
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only superadmins can reveal agent bootstrap keys",
        )

    # Explicit local-auth gate. External-auth users (LDAP / OIDC /
    # SAML / RADIUS / TACACS+) don't have a local password to confirm,
    # so the password-re-verify flow is meaningless for them. Rather
    # than letting it accidentally pass (or fail with a confusing
    # "password incorrect" when in fact the user has no local
    # password), surface the policy explicitly: "log in as a local
    # admin to reveal these keys."
    if current_user.auth_source != "local":
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="agent_keys_reveal_denied",
                resource_type="platform",
                resource_id="agent-keys",
                resource_display="agent bootstrap keys",
                result="forbidden",
                new_value={"reason": "external_auth"},
            )
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Agent bootstrap keys can only be revealed by a local-auth "
            "superadmin (your account authenticates via "
            f"{current_user.auth_source}). Log in as a local admin to "
            "reveal these keys, or pass them out-of-band — they also "
            "live in /etc/spatiumddi/.env on the control plane host.",
        )

    if not current_user.hashed_password or not verify_password(
        body.password, current_user.hashed_password
    ):
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="agent_keys_reveal_denied",
                resource_type="platform",
                resource_id="agent-keys",
                resource_display="agent bootstrap keys",
                result="bad_password",
                new_value={"reason": "password_mismatch"},
            )
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Password is incorrect",
        )

    # Success — emit an audit row carrying NOTHING about the key
    # values themselves (audit rows are themselves operator-visible
    # via the audit log surface). Just "this user revealed the
    # bootstrap keys at this time".
    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="agent_keys_revealed",
            resource_type="platform",
            resource_id="agent-keys",
            resource_display="agent bootstrap keys",
            result="success",
        )
    )
    await db.commit()
    logger.info(
        "agent_keys_revealed",
        user=current_user.username,
        dns_configured=bool(settings.dns_agent_key),
        dhcp_configured=bool(settings.dhcp_agent_key),
    )

    return RevealResponse(
        dns_agent_key=settings.dns_agent_key or "",
        dhcp_agent_key=settings.dhcp_agent_key or "",
        dns_agent_configured=bool(settings.dns_agent_key),
        dhcp_agent_configured=bool(settings.dhcp_agent_key),
    )
