"""Redis-backed throttles for the auth surface.

Two complements to the per-account lockout (#71):

* ``login_rate_limited`` — a per-source-IP attempt budget on
  ``/auth/login`` + ``/auth/login/mfa`` (#4). Account lockout needs a
  *valid username* to engage, so an attacker spraying usernames gets one
  free guess each before lockout triggers; an IP budget caps that.
* ``mfa_challenge_consume`` — single-use consumption of an MFA challenge
  ``jti`` so a captured (challenge + TOTP) pair can't be replayed inside
  the 5-minute token TTL (#7).

Both **fail open** when Redis is unreachable: the per-account lockout +
the always-required second factor remain the hard backstops, so a Redis
outage degrades these to no-ops rather than locking everyone out.
"""

from __future__ import annotations

import structlog

from app.config import settings
from app.core.redis_client import make_async_redis

logger = structlog.get_logger(__name__)

# 30 attempts / 60 s / IP. Generous enough that a NAT'd office or a
# password-manager retry storm won't trip it, tight enough to throttle
# scripted username spraying.
_LOGIN_RL_MAX = 30
_LOGIN_RL_WINDOW_SECONDS = 60

# Matches create_mfa_challenge_token's _MFA_TOKEN_TTL_MINUTES (5 min) —
# once the challenge JWT expires the used-marker is moot.
_MFA_USED_TTL_SECONDS = 5 * 60


async def login_rate_limited(ip: str | None) -> bool:
    """Increment the per-IP login counter and return True once it exceeds
    the window budget. Fails open (False) when ``ip`` is unknown or Redis
    is unavailable."""
    if not ip:
        return False
    key = f"login_rl:{ip}"
    try:
        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        try:
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, _LOGIN_RL_WINDOW_SECONDS)
            return int(count) > _LOGIN_RL_MAX
        finally:
            await r.aclose()
    except Exception as exc:  # noqa: BLE001 — throttle must never break login
        logger.warning("login_rate_limit_redis_unavailable", error=str(exc))
        return False


async def mfa_challenge_consume(jti: str | None) -> bool:
    """Atomically claim an MFA challenge ``jti``. Returns True if this is
    the first claim (caller may proceed), False if it was already consumed
    (replay / race — caller must reject).

    Fails open (True) when ``jti`` is missing (legacy token) or Redis is
    unavailable, preserving the prior stateless behaviour rather than
    blocking a legitimate MFA login during a Redis outage."""
    if not jti:
        return True
    key = f"mfa_challenge:{jti}:used"
    try:
        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        try:
            # SET key 1 EX ttl NX — returns truthy only when the key did
            # not already exist, i.e. this is the first (winning) claim.
            ok = await r.set(key, "1", ex=_MFA_USED_TTL_SECONDS, nx=True)
            return bool(ok)
        finally:
            await r.aclose()
    except Exception as exc:  # noqa: BLE001 — never block MFA on a Redis blip
        logger.warning("mfa_replay_guard_redis_unavailable", error=str(exc))
        return True
