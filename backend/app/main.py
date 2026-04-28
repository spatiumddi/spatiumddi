import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
import structlog.contextvars
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.health import router as health_router
from app.api.v1.router import api_v1_router
from app.config import settings
from app.log import configure_logging
from app.metrics import PrometheusMiddleware, metrics_endpoint

# Import for side-effect: registers the SQLAlchemy after_commit listener
# that forwards audit events to syslog + webhook targets. Must run at app
# startup so the listener is attached before any request handler writes
# an AuditLog row.
from app.services import audit_forward  # noqa: F401

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


# Built-in roles installed on first boot. Shape matches docs/PERMISSIONS.md.
# Keys are the role names; the tuple is (description, permissions).
_BUILTIN_ROLES: dict[str, tuple[str, list[dict[str, object]]]] = {
    "Superadmin": (
        "Full control — wildcard on all actions and resources.",
        [{"action": "*", "resource_type": "*"}],
    ),
    "Viewer": (
        "Read-only access to every resource.",
        [{"action": "read", "resource_type": "*"}],
    ),
    "IPAM Editor": (
        "Full CRUD on IPAM objects (spaces, blocks, subnets, addresses, VLANs, "
        "NAT mappings, custom fields).",
        [
            {"action": "admin", "resource_type": "ip_space"},
            {"action": "admin", "resource_type": "ip_block"},
            {"action": "admin", "resource_type": "subnet"},
            {"action": "admin", "resource_type": "ip_address"},
            {"action": "admin", "resource_type": "vlan"},
            {"action": "admin", "resource_type": "nat_mapping"},
            {"action": "admin", "resource_type": "custom_field"},
        ],
    ),
    "DNS Editor": (
        "Full CRUD on DNS zones, records, server groups and blocklists.",
        [
            {"action": "admin", "resource_type": "dns_group"},
            {"action": "admin", "resource_type": "dns_zone"},
            {"action": "admin", "resource_type": "dns_record"},
            {"action": "admin", "resource_type": "dns_blocklist"},
        ],
    ),
    "DHCP Editor": (
        "Full CRUD on DHCP servers, scopes, pools, statics, client classes, and MAC blocks.",
        [
            {"action": "admin", "resource_type": "dhcp_server"},
            {"action": "admin", "resource_type": "dhcp_scope"},
            {"action": "admin", "resource_type": "dhcp_pool"},
            {"action": "admin", "resource_type": "dhcp_static"},
            {"action": "admin", "resource_type": "dhcp_client_class"},
            {"action": "admin", "resource_type": "dhcp_mac_block"},
        ],
    ),
    "Network Editor": (
        "Full CRUD on SNMP-polled network devices (routers, switches, APs).",
        [
            {"action": "admin", "resource_type": "manage_network_devices"},
        ],
    ),
}


async def _seed_builtin_roles() -> None:
    """Insert built-in roles on first start; refresh their permissions on every boot.

    The permissions on built-in roles are owned by the code, not the admin UI —
    if the role already exists we still overwrite `permissions` and `description`
    so upgrades ship new resource types without a manual edit. `name` is used
    as the stable identity; admins who want to tweak built-in permission sets
    should clone the role first.
    """
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models.auth import Role

    async with AsyncSessionLocal() as session:
        try:
            for name, (description, perms) in _BUILTIN_ROLES.items():
                existing = await session.scalar(select(Role).where(Role.name == name))
                if existing is None:
                    session.add(
                        Role(
                            name=name,
                            description=description,
                            is_builtin=True,
                            permissions=perms,
                        )
                    )
                else:
                    existing.description = description
                    existing.permissions = perms
                    existing.is_builtin = True
            await session.commit()
        except Exception as exc:
            logger.debug("builtin_roles_seed_skipped", reason=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("startup", service="api", version="0.1.0", debug=settings.debug)
    await _seed_default_admin()
    await _seed_builtin_roles()
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
