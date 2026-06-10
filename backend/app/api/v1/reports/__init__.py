"""Top-N reports surface (issue #47).

Four read-only aggregation endpoints, each ranking existing entity
tables into the "what are my biggest / busiest / most-touched things?"
shape operators of comparable tools (NetBox, phpIPAM, Infoblox) expect.

Everything is derived server-side from tables that already exist — no
new model, no migration. The feature module ``reports.top_n`` gates the
whole router (default-enabled, no seed row needed).

Routes:

* ``GET /reports/top-subnets-by-utilization`` — subnets ranked by
  ``utilization_percent``.
* ``GET /reports/top-owners-by-ip-count`` — Customers ranked by the
  count of IPs in their subnets (plus an "Unowned" bucket).
* ``GET /reports/top-modified-resources`` — audit-log rows grouped by
  resource over the trailing 7 days.
* ``GET /reports/top-dns-clients`` — DNS query-log clients ranked by
  query volume.
"""

from fastapi import APIRouter

from app.api.v1.reports.router import router as reports_inner_router

router = APIRouter()
router.include_router(reports_inner_router)

__all__ = ["router"]
