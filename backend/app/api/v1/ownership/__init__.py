"""Logical ownership entities (issue #91).

Three thin CRUD surfaces (Customer / Site / Provider) that share the
same audit helper and follow the same shape as the ASN router.
"""

from app.api.v1.ownership.customers import router as customers_router
from app.api.v1.ownership.providers import router as providers_router
from app.api.v1.ownership.sites import router as sites_router

__all__ = ["customers_router", "providers_router", "sites_router"]
