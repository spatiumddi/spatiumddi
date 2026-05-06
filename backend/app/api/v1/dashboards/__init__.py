"""Dashboard rollup endpoints (issues #107 / #108 / #109).

One endpoint per dashboard tab that aggregates several entity tables
into the shape the home page panel needs. Server-side rollup avoids
N+1 fetches across the dashboard (especially expensive for #107's
RPKI ROA + overlay simulate cross-entity walk) and keeps the
front-end down to a single React Query + refresh tick per tab.

Routes:

* ``GET /dashboards/network/summary``  — Network tab (#107)
* ``GET /dashboards/integrations/summary``  — Integrations tab (#108)
* ``GET /dashboards/security/summary``  — Security tab (#109)
"""

from fastapi import APIRouter

from app.api.v1.dashboards.integrations import router as integrations_router
from app.api.v1.dashboards.network import router as network_router
from app.api.v1.dashboards.security import router as security_router

router = APIRouter()
router.include_router(network_router)
router.include_router(integrations_router)
router.include_router(security_router)

__all__ = ["router"]
