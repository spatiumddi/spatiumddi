"""Health probe endpoints consumed by Docker/Kubernetes."""

import structlog
from fastapi import APIRouter
from sqlalchemy import text
from starlette import status
from starlette.responses import JSONResponse

from app.db import AsyncSessionLocal

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict:
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """
    Readiness probe — checks DB and Redis connectivity.
    Returns 200 if ready, 503 with failed-check details if not.
    """
    checks: dict[str, str] = {}
    healthy = True

    # Database check
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness_check_failed", check="database", error=str(exc))
        checks["database"] = f"error: {exc}"
        healthy = False

    # Redis check
    try:
        import redis.asyncio as aioredis

        from app.config import settings

        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("readiness_check_failed", check="redis", error=str(exc))
        checks["redis"] = f"error: {exc}"
        healthy = False

    body = {"status": "ok" if healthy else "degraded", "checks": checks}
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=body)


@router.get("/health/startup")
async def startup() -> JSONResponse:
    """Startup probe (Kubernetes slow-start containers) — same logic as readiness."""
    return await readiness()
