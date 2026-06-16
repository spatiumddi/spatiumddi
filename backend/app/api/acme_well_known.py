"""Unauthenticated ACME http-01 well-known endpoint (issue #438 Phase 4).

Mounted at the app ROOT (not under ``/api/v1``, no auth, no feature-module
gate) because the public CA fetches it anonymously during validation:

    GET /.well-known/acme-challenge/<token>  ->  "<token>.<thumbprint>"

It serves only the key-authorization the orchestrator published for a
live http-01 challenge (404 otherwise) — it exposes no secret material
and no operator data. The frontend nginx proxies this path to the api so
the CA reaches it at the validated FQDN over :80/:443.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse

from app.api.deps import DB
from app.services.acme_client import http01

router = APIRouter()


@router.get(
    "/.well-known/acme-challenge/{token}",
    include_in_schema=False,
    response_class=PlainTextResponse,
)
async def serve_http01_challenge(token: str, db: DB) -> PlainTextResponse:
    key_authorization = await http01.lookup(db, token)
    if key_authorization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown ACME challenge token")
    return PlainTextResponse(key_authorization)
