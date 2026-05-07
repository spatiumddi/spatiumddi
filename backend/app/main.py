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
from app.services import (
    audit_forward,  # noqa: F401
    event_publisher,  # noqa: F401
)

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
        "NAT mappings, custom fields, IPAM templates) plus the logical "
        "ownership tags (customer / site / provider) IPAM rows reference and "
        "the customer-deliverable services (#94) those bundles belong to.",
        [
            {"action": "admin", "resource_type": "ip_space"},
            {"action": "admin", "resource_type": "ip_block"},
            {"action": "admin", "resource_type": "subnet"},
            {"action": "admin", "resource_type": "ip_address"},
            {"action": "admin", "resource_type": "vlan"},
            {"action": "admin", "resource_type": "nat_mapping"},
            {"action": "admin", "resource_type": "custom_field"},
            {"action": "admin", "resource_type": "manage_ipam_templates"},
            {"action": "admin", "resource_type": "customer"},
            {"action": "admin", "resource_type": "site"},
            {"action": "admin", "resource_type": "provider"},
            {"action": "admin", "resource_type": "network_service"},
        ],
    ),
    "DNS Editor": (
        "Full CRUD on DNS zones, records, server groups, blocklists, and pools.",
        [
            {"action": "admin", "resource_type": "dns_group"},
            {"action": "admin", "resource_type": "dns_zone"},
            {"action": "admin", "resource_type": "dns_record"},
            {"action": "admin", "resource_type": "dns_blocklist"},
            {"action": "admin", "resource_type": "manage_dns_pools"},
        ],
    ),
    "DHCP Editor": (
        "Full CRUD on DHCP servers, scopes, pools, statics, client classes, "
        "option templates, and MAC blocks.",
        [
            {"action": "admin", "resource_type": "dhcp_server"},
            {"action": "admin", "resource_type": "dhcp_scope"},
            {"action": "admin", "resource_type": "dhcp_pool"},
            {"action": "admin", "resource_type": "dhcp_static"},
            {"action": "admin", "resource_type": "dhcp_client_class"},
            {"action": "admin", "resource_type": "dhcp_option_template"},
            {"action": "admin", "resource_type": "dhcp_mac_block"},
        ],
    ),
    "Network Editor": (
        "Full CRUD on SNMP-polled network devices (routers, switches, APs), "
        "on-demand nmap scans, the ASN registry, VRFs, WAN circuits, "
        "SD-WAN overlay topology + routing policies + the application "
        "catalog (#95), the customer-deliverable services (#94) those "
        "resources bundle into, and the logical ownership tags (customer "
        "/ site / provider) those entities reference.",
        [
            {"action": "admin", "resource_type": "manage_network_devices"},
            {"action": "admin", "resource_type": "manage_nmap_scans"},
            {"action": "admin", "resource_type": "manage_asns"},
            {"action": "admin", "resource_type": "vrf"},
            {"action": "admin", "resource_type": "circuit"},
            {"action": "admin", "resource_type": "network_service"},
            {"action": "admin", "resource_type": "overlay_network"},
            {"action": "admin", "resource_type": "routing_policy"},
            {"action": "admin", "resource_type": "application_category"},
            {"action": "admin", "resource_type": "customer"},
            {"action": "admin", "resource_type": "site"},
            {"action": "admin", "resource_type": "provider"},
        ],
    ),
    "Auditor": (
        "Read-only on conformity evaluations (issue #106) plus read on "
        "audit log + classifications. Suitable for an external auditor "
        "account that should be able to pull the conformity PDF and "
        "verify the underlying evidence without making changes.",
        [
            {"action": "read", "resource_type": "conformity"},
            {"action": "read", "resource_type": "audit"},
            {"action": "read", "resource_type": "subnet"},
            {"action": "read", "resource_type": "ip_address"},
            {"action": "read", "resource_type": "dns_zone"},
            {"action": "read", "resource_type": "dhcp_scope"},
        ],
    ),
    "Compliance Editor": (
        "Full CRUD on conformity policies (issue #106) plus read on the "
        "underlying resources (subnets / IPs / zones / scopes) so the "
        "compliance team can author + tune policies without touching "
        "operational config.",
        [
            {"action": "admin", "resource_type": "conformity"},
            {"action": "read", "resource_type": "audit"},
            {"action": "read", "resource_type": "subnet"},
            {"action": "read", "resource_type": "ip_address"},
            {"action": "read", "resource_type": "dns_zone"},
            {"action": "read", "resource_type": "dhcp_scope"},
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
    # Standard / well-known BGP communities (RFC 1997 / 7611 / 7999).
    # Idempotent; failure-tolerant.
    try:
        from app.services.bgp_communities import (  # noqa: PLC0415
            seed_standard_communities,
        )

        await seed_standard_communities()
    except Exception as exc:  # noqa: BLE001
        logger.debug("bgp_communities_seed_skipped", reason=str(exc))
    # Curated SD-WAN application catalog (Office365, Zoom, Slack,
    # GitHub, …). Idempotent; failure-tolerant.
    try:
        from app.services.applications import (  # noqa: PLC0415
            seed_builtin_applications,
        )

        await seed_builtin_applications()
    except Exception as exc:  # noqa: BLE001
        logger.debug("builtin_applications_seed_skipped", reason=str(exc))
    # Compliance-change alert rules — three disabled stubs (PCI /
    # HIPAA / internet-facing). Issue #105. Idempotent;
    # failure-tolerant. The seeder only inserts a row when no rule
    # of the matching ``rule_type + classification`` already exists,
    # so operators who toggled / customised one are never overridden.
    try:
        from app.services.alerts import (  # noqa: PLC0415
            seed_builtin_compliance_alert_rules,
        )

        await seed_builtin_compliance_alert_rules()
    except Exception as exc:  # noqa: BLE001
        logger.debug("compliance_alert_rules_seed_skipped", reason=str(exc))
    # Conformity policies — eight starter rows covering PCI / HIPAA /
    # internet-facing / SOC2 (issue #106). Disabled by default so
    # they don't burn cycles until the operator opts in.
    # Idempotent; failure-tolerant.
    try:
        from app.services.conformity import (  # noqa: PLC0415
            seed_builtin_conformity_policies,
        )

        await seed_builtin_conformity_policies()
    except Exception as exc:  # noqa: BLE001
        logger.debug("conformity_policies_seed_skipped", reason=str(exc))
    # Audit-chain-broken alert rule — singleton, enabled by default
    # (issue #73). Idempotent; if an operator has disabled it the
    # seeder doesn't re-flip the toggle.
    try:
        from app.services.alerts import seed_audit_chain_alert_rule  # noqa: PLC0415

        await seed_audit_chain_alert_rule()
    except Exception as exc:  # noqa: BLE001
        logger.debug("audit_chain_alert_rule_seed_skipped", reason=str(exc))
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

    # Transient-DB-connection handler (issue #117). When a backup
    # restore disposes the engine + ``pg_terminate_backend``s every
    # connection, requests that had ALREADY checked one out get
    # their underlying socket killed mid-flight. ``pool_pre_ping``
    # only fires at checkout, so it can't recover those — they
    # surface as ``InterfaceError: cannot call PreparedStatement
    # .fetch(): the underlying connection is closed`` (or
    # ``OperationalError`` for similar pool-level failures).
    #
    # Same self-healing logic applies: the very next request from
    # the same caller will go through pool_pre_ping and succeed.
    # Convert to a clean 503 + ``Retry-After: 1`` so agent
    # long-polls back off and retry instead of cascading the
    # failure into the diagnostics surface.
    #
    # Registered BEFORE the broader Exception handler so connection-
    # closed errors take this path and skip the unhandled-exception
    # capture (they're transient noise, not real bugs).
    from sqlalchemy.exc import (  # noqa: PLC0415
        InterfaceError as SAInterfaceError,
    )
    from sqlalchemy.exc import (
        OperationalError as SAOperationalError,
    )

    @app.exception_handler(SAInterfaceError)
    @app.exception_handler(SAOperationalError)
    async def _transient_db_connection(request: Request, exc: Exception) -> Response:
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        text = str(exc).lower()
        is_connection_closed = (
            "connection is closed" in text
            or "connection was closed" in text
            or "connection lost" in text
            or "server closed the connection unexpectedly" in text
        )
        if not is_connection_closed:
            # Some other InterfaceError — let it fall through to
            # the unhandled-exception path so it gets captured.
            raise exc
        logger.info(
            "db_connection_closed_transient",
            method=request.method,
            path=request.url.path,
            error=str(exc)[:200],
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Database connection was closed mid-request "
                    "(likely a backup restore in progress). Retry."
                )
            },
            headers={"Retry-After": "1"},
        )

    # Unhandled-exception capture (issue #123). Registered last so it
    # only catches what slipped past every other handler — auth /
    # permission / validation errors raise typed HTTPException
    # subclasses that FastAPI's own machinery turns into 4xx
    # responses without ever hitting this path.
    @app.exception_handler(Exception)
    async def _capture_unhandled(request: Request, exc: Exception) -> Response:
        # Lazy imports — keep app boot-time import graph small + avoid
        # circulars (services/diagnostics imports models which imports
        # base which Alembic also imports at migration time).
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        from app.db import AsyncSessionLocal  # noqa: PLC0415
        from app.services.diagnostics import (  # noqa: PLC0415
            record_unhandled_exception_async,
        )

        request_id = request.headers.get("X-Request-ID")
        try:
            sanitised_headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in {"authorization", "cookie", "x-api-token"}
            }
        except Exception:
            sanitised_headers = {}
        context = {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "headers": sanitised_headers,
            "client": request.client.host if request.client else None,
        }
        try:
            async with AsyncSessionLocal() as db:
                await record_unhandled_exception_async(
                    db,
                    service="api",
                    exc=exc,
                    route_or_task=f"{request.method} {request.url.path}",
                    request_id=request_id,
                    context=context,
                )
        except Exception:
            # Capture failures must never replace the original
            # exception's response. Eat it.
            pass
        logger.exception(
            "unhandled_exception",
            method=request.method,
            path=request.url.path,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error"},
        )

    return app


app = create_app()
