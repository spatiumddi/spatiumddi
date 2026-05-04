"""MCP (Model Context Protocol) HTTP endpoint (issue #90 Wave 2).

JSON-RPC 2.0 over HTTP, mirroring the MCP spec's "Streamable HTTP"
transport. Wave 2 implements the minimum viable surface — operators
can connect Claude Desktop / Cursor / any MCP-speaking client and
call SpatiumDDI's read-only tools.

Methods supported:
    initialize        — protocol handshake
    notifications/initialized  — client-pushed init complete
    tools/list        — list available tools
    tools/call        — invoke a tool with arguments
    ping              — health check

Methods deliberately NOT supported in Wave 2 (return method-not-found):
    resources/*, prompts/*, sampling/*, completion/*

Auth:
    The same auth surface as every other ``/api/v1/*`` route — session
    JWT for browser clients, API tokens for external MCP clients.
    External clients use a token with the ``read`` scope (Wave 2 tools
    are all read-only). The scope helper in ``app/services/api_token_scopes``
    has been extended to allow POST to ``/api/v1/mcp/*`` under ``read``
    so this works without a dedicated MCP scope. Phase 3 adds an
    ``mcp:write`` scope when write tools land.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.api.deps import DB, CurrentUser
from app.services.ai.tools import (
    REGISTRY,
    ToolArgumentError,
    ToolNotFound,
)

logger = structlog.get_logger(__name__)
router = APIRouter()


# Server identity returned in ``initialize``. Bump ``version`` when the
# tool surface changes shape in a non-additive way.
_SERVER_INFO = {
    "name": "spatiumddi",
    "version": "1.0.0",
}

# Protocol version we speak. Matches the MCP spec rev we built against;
# clients negotiate down if they speak an older version.
_PROTOCOL_VERSION = "2025-06-18"


class JSONRPCRequest(BaseModel):
    """One JSON-RPC 2.0 request frame. ``id`` is None for notifications."""

    model_config = {"extra": "allow"}

    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] | None = None


def _ok(req_id: int | str | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: int | str | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": payload}


# Standard JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


@router.get("")
async def mcp_get(current_user: CurrentUser) -> dict[str, Any]:
    """A bare GET on ``/mcp`` returns server info — useful for browser
    sanity-checks and for Streamable-HTTP clients that probe for
    server identity without going through ``initialize``. Auth is
    required to keep server info from leaking to anonymous probes.
    """
    return {
        "server": _SERVER_INFO,
        "protocol_version": _PROTOCOL_VERSION,
        "available_tools": len(REGISTRY.read_only()),
        "transport": "streamable_http",
    }


@router.post("")
async def mcp_post(
    request: Request, current_user: CurrentUser, db: DB
) -> dict[str, Any] | list[dict[str, Any]]:
    """Handle one JSON-RPC request (or batch). Returns a single
    response object for single requests, a list for batches. Notifications
    (``id`` omitted) get no response — we still emit an empty object so
    HTTP clients always have a body to parse.
    """
    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid JSON body — {exc}",
        ) from None

    if isinstance(raw, list):
        return [await _dispatch_one(item, db, current_user) for item in raw]
    return await _dispatch_one(raw, db, current_user)


async def _dispatch_one(raw: Any, db: Any, user: Any) -> dict[str, Any]:
    try:
        req = JSONRPCRequest.model_validate(raw)
    except Exception as exc:
        return _err(None, _INVALID_REQUEST, f"invalid JSON-RPC frame — {exc}")

    method = req.method
    params = req.params or {}
    started = time.monotonic()

    try:
        if method == "initialize":
            client_info = params.get("clientInfo", {})
            logger.info(
                "mcp_initialize",
                client_name=client_info.get("name"),
                client_version=client_info.get("version"),
                protocol_version=params.get("protocolVersion"),
            )
            return _ok(
                req.id,
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "serverInfo": _SERVER_INFO,
                    "capabilities": {
                        # Wave 2 only ships ``tools``. ``resources`` /
                        # ``prompts`` / ``sampling`` advertise as absent
                        # so capable clients don't try to call them.
                        "tools": {},
                    },
                },
            )

        if method in ("notifications/initialized", "ping"):
            # Notifications don't get a response per JSON-RPC 2.0,
            # but we return an empty result to keep our HTTP shape
            # uniform — clients ignore the result for notifications.
            return _ok(req.id, {})

        if method == "tools/list":
            return _ok(
                req.id,
                {"tools": [t.to_mcp_tool() for t in REGISTRY.read_only()]},
            )

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                return _err(
                    req.id,
                    _INVALID_PARAMS,
                    "tools/call requires string `name`",
                )
            try:
                result = await REGISTRY.call(name, arguments, db=db, user=user)
            except ToolNotFound as exc:
                return _err(
                    req.id,
                    _METHOD_NOT_FOUND,
                    f"tool not found: {exc.name!r}",
                )
            except ToolArgumentError as exc:
                return _err(
                    req.id,
                    _INVALID_PARAMS,
                    f"invalid arguments for tool {exc.name!r}: {exc.detail}",
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "mcp_tool_call",
                tool=name,
                user_id=str(user.id),
                latency_ms=elapsed_ms,
            )
            # MCP wraps tool results in a ``content`` array of typed
            # blocks. JSON results go in a single text block — most
            # clients then re-parse the JSON before showing it.
            import json

            return _ok(
                req.id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, default=str),
                        }
                    ],
                    "isError": False,
                },
            )

        # Spec-mandated method-not-found for anything else.
        return _err(
            req.id,
            _METHOD_NOT_FOUND,
            f"method not implemented: {method}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("mcp_internal_error", method=method)
        return _err(
            req.id,
            _INTERNAL_ERROR,
            f"internal error — {type(exc).__name__}: {exc}",
        )
