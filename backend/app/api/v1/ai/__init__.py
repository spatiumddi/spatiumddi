"""Operator Copilot API surface (issue #90).

Re-exports a single ``router`` that combines:
    /api/v1/ai/providers/...  — provider config CRUD (Wave 1)
    /api/v1/ai/tools          — tool catalog introspection (Wave 2)
    /api/v1/ai/mcp            — MCP JSON-RPC endpoint (Wave 2)

Future waves will mount additional sub-routers here:
    /api/v1/ai/chat           — Wave 3
    /api/v1/ai/sessions       — Wave 3
"""

from fastapi import APIRouter

from app.api.v1.ai.mcp import router as mcp_router
from app.api.v1.ai.providers import router as providers_router
from app.api.v1.ai.tools import router as tools_router

router = APIRouter()
router.include_router(providers_router)
router.include_router(tools_router)
router.include_router(mcp_router, prefix="/mcp")

__all__ = ["router"]
