import uuid

import structlog
import structlog.contextvars
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.log import configure_logging
from app.metrics import PrometheusMiddleware, metrics_endpoint
from app.api.health import router as health_router
from app.api.v1.router import api_v1_router

logger = structlog.get_logger(__name__)


async def _seed_default_admin() -> None:
    """Create the default admin user if no users exist yet."""
    from sqlalchemy import func, select
    from app.core.security import hash_password
    from app.db import AsyncSessionLocal
    from app.models.auth import User

    async with AsyncSessionLocal() as session:
        try:
            count = await session.scalar(select(func.count()).select_from(User))
            if count == 0:
                admin = User(
                    username="admin",
                    email="admin@localhost",
                    display_name="Administrator",
                    hashed_password=hash_password("admin"),
                    is_superadmin=True,
                    is_active=True,
                    auth_source="local",
                    force_password_change=True,
                )
                session.add(admin)
                await session.commit()
                logger.warning(
                    "default_admin_created",
                    username="admin",
                    message="Default admin created with password 'admin' — change it immediately",
                )
        except Exception as exc:
            # Table may not exist yet (pre-migration). Skip silently.
            logger.debug("default_admin_seed_skipped", reason=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("startup", service="api", version="0.1.0", debug=settings.debug)
    await _seed_default_admin()
    yield
    logger.info("shutdown", service="api")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request_id to structlog context for every request."""

    async def dispatch(self, request: Request, call_next: object) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            service="api",
        )
        response: Response = await call_next(request)  # type: ignore[arg-type]
        response.headers["X-Request-ID"] = request_id
        return response


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_title,
        description="Open-source DDI — DNS, DHCP, and IP Address Management",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # Middleware (outermost first)
    app.add_middleware(RequestContextMiddleware)

    if settings.prometheus_metrics_enabled:
        app.add_middleware(PrometheusMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routes
    app.include_router(health_router)
    app.include_router(api_v1_router, prefix="/api/v1")

    if settings.prometheus_metrics_enabled:
        app.add_route("/metrics", metrics_endpoint)

    return app


app = create_app()
