"""DHCP API router aggregation."""

from fastapi import APIRouter

from app.api.v1.dhcp.agents import router as agents_router
from app.api.v1.dhcp.client_classes import router as client_classes_router
from app.api.v1.dhcp.mac_blocks import router as mac_blocks_router
from app.api.v1.dhcp.pools import router as pools_router
from app.api.v1.dhcp.scopes import router as scopes_router
from app.api.v1.dhcp.server_groups import router as server_groups_router
from app.api.v1.dhcp.servers import router as servers_router
from app.api.v1.dhcp.statics import router as statics_router

router = APIRouter()
router.include_router(server_groups_router)
router.include_router(servers_router)
router.include_router(scopes_router)
router.include_router(pools_router)
router.include_router(statics_router)
router.include_router(client_classes_router)
router.include_router(mac_blocks_router)
router.include_router(agents_router)

__all__ = ["router"]
