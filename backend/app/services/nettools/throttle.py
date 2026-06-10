"""Per-user rate limiting for the built-in network tools (issue #58).

The network-tools surface fires real packets (ping floods, traceroute
storms) and off-prem traffic (whois, DNS propagation against public
resolvers). A per-user token bucket caps how often a single operator can
hammer the surface — protects both the api container and the operator's
own egress reputation.

Two budgets:

* ``DEFAULT`` — the on-prem tools (ping / traceroute / mtr / dig /
  port-test / tls-cert / mac-vendor). 20 calls / 60 s.
* ``OFFPREM`` — whois + DNS propagation. Tighter (8 calls / 60 s)
  because these leave the network and hit shared public infrastructure.

Mirrors :mod:`app.core.auth_throttle`: a fixed-window counter in Redis,
**fail-open** when Redis is unreachable so a cache blip never bricks the
tools page. Returned as FastAPI dependency factories — attach one per
endpoint.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import Depends, HTTPException, status

from app.api.deps import CurrentUser
from app.config import settings
from app.core.redis_client import make_async_redis
from app.models.auth import User

logger = structlog.get_logger(__name__)

_WINDOW_SECONDS: Final[int] = 60

# Budget per fixed window, keyed by "bucket" name. The bucket lets us
# share one window across the on-prem tools while giving the off-prem
# tools their own tighter counter.
_BUDGETS: Final[dict[str, int]] = {
    "default": 20,
    "offprem": 8,
}


async def _consume(user_id: str, bucket: str) -> bool:
    """Increment the per-user/bucket counter; return True once it exceeds
    the window budget. Fails open (False) when Redis is unavailable."""
    budget = _BUDGETS.get(bucket, _BUDGETS["default"])
    key = f"nettools_rl:{bucket}:{user_id}"
    try:
        r = make_async_redis(settings.redis_url, socket_connect_timeout=2)
        try:
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, _WINDOW_SECONDS)
            return int(count) > budget
        finally:
            await r.aclose()
    except Exception as exc:  # noqa: BLE001 — throttle must never break the tool
        logger.warning("nettools_rate_limit_redis_unavailable", error=str(exc))
        return False


def rate_limit(bucket: str = "default"):
    """Build a FastAPI dependency enforcing the per-user token bucket for
    ``bucket``. Raises 429 with a ``Retry-After`` header when the budget
    is exceeded.

    Superadmins are NOT exempt — the limit protects the egress path, not
    a permission boundary, so an admin running a tight loop is exactly
    what we want to throttle.
    """
    if bucket not in _BUDGETS:
        raise RuntimeError(f"rate_limit: unknown bucket {bucket!r}")

    async def _dep(current_user: CurrentUser) -> User:
        if await _consume(str(current_user.id), bucket):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded — max {_BUDGETS[bucket]} "
                    f"{bucket} network-tool calls per {_WINDOW_SECONDS}s. "
                    "Wait a moment and retry."
                ),
                headers={"Retry-After": str(_WINDOW_SECONDS)},
            )
        return current_user

    return _dep


# Pre-built dependencies for the two budgets so routes read cleanly.
RateLimitDefault = Depends(rate_limit("default"))
RateLimitOffprem = Depends(rate_limit("offprem"))


__all__ = ["RateLimitDefault", "RateLimitOffprem", "rate_limit"]
