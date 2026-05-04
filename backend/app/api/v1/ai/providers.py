"""Admin CRUD for LLM providers (issue #90 — Operator Copilot Wave 1).

Mirrors ``app.api.v1.auth_providers.router``: secrets are stored
Fernet-encrypted via ``app.core.crypto`` and never returned to the
client (a ``has_api_key`` boolean is surfaced instead). A PUT that
omits ``api_key`` leaves the stored ciphertext untouched; passing
``api_key: ""`` clears it.

All routes require superadmin in Wave 1. A future wave can introduce
a ``manage_ai`` permission seeded into a non-Superadmin builtin role.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, SuperAdmin
from app.api.v1.dhcp._audit import write_audit
from app.core.crypto import encrypt_str
from app.drivers.llm import get_driver
from app.drivers.llm.registry import known_kinds
from app.models.ai import AI_PROVIDER_KINDS, AIProvider

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────


class ProviderCreate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str = Field(min_length=1, max_length=255)
    kind: str
    base_url: str = ""
    # ``None`` means \"no API key supplied\" (cleared). Non-empty
    # encrypts on save. Empty string explicitly clears.
    api_key: str | None = None
    default_model: str = ""
    is_enabled: bool = False
    priority: int = 100
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _v_kind(cls, v: str) -> str:
        if v not in AI_PROVIDER_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(AI_PROVIDER_KINDS))}")
        if v not in known_kinds():
            raise ValueError(
                f"kind {v!r} has no driver registered. "
                f"Wave 1 ships only: {', '.join(known_kinds())}"
            )
        return v


class ProviderUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = None
    # None → leave key untouched. ``""`` → clear. Non-empty → replace.
    api_key: str | None = None
    default_model: str | None = None
    is_enabled: bool | None = None
    priority: int | None = None
    options: dict[str, Any] | None = None


class ProviderResponse(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    base_url: str
    has_api_key: bool
    default_model: str
    is_enabled: bool
    priority: int
    options: dict[str, Any]
    created_at: datetime
    modified_at: datetime


class TestConnectionRequest(BaseModel):
    """Body for the unsaved-test-connection endpoint. Mirrors the
    ``ProviderCreate`` shape minus the id-affecting fields.
    """

    kind: str
    base_url: str = ""
    api_key: str | None = None
    default_model: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class TestConnectionResponse(BaseModel):
    ok: bool
    detail: str
    latency_ms: int | None
    sample_models: list[str]


class ListModelsResponse(BaseModel):
    models: list[dict[str, Any]]


# ── Helpers ──────────────────────────────────────────────────────────────


def _to_response(p: AIProvider) -> ProviderResponse:
    return ProviderResponse(
        id=p.id,
        name=p.name,
        kind=p.kind,
        base_url=p.base_url,
        has_api_key=p.api_key_encrypted is not None,
        default_model=p.default_model,
        is_enabled=p.is_enabled,
        priority=p.priority,
        options=p.options or {},
        created_at=p.created_at,
        modified_at=p.modified_at,
    )


class _EphemeralProvider:
    """Stand-in for an :class:`AIProvider` row used by the unsaved
    test-connection endpoint — lets us instantiate a driver without
    persisting the config first.
    """

    def __init__(
        self,
        kind: str,
        base_url: str,
        api_key: str | None,
        options: dict[str, Any],
    ) -> None:
        self.kind = kind
        self.base_url = base_url
        # The driver decrypts via ``decrypt_str``; mimic the same
        # encrypted-bytes shape so it works without code branches.
        self.api_key_encrypted = encrypt_str(api_key) if api_key else None
        self.options = options or {}


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/providers", response_model=list[ProviderResponse])
async def list_providers(current_user: SuperAdmin, db: DB) -> list[ProviderResponse]:
    rows = (
        (
            await db.execute(
                select(AIProvider).order_by(AIProvider.priority.asc(), AIProvider.name.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_response(p) for p in rows]


@router.post(
    "/providers",
    response_model=ProviderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider(
    body: ProviderCreate, current_user: SuperAdmin, db: DB
) -> ProviderResponse:
    row = AIProvider(
        name=body.name,
        kind=body.kind,
        base_url=body.base_url,
        api_key_encrypted=encrypt_str(body.api_key) if body.api_key else None,
        default_model=body.default_model,
        is_enabled=body.is_enabled,
        priority=body.priority,
        options=body.options,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"AI provider name {body.name!r} already exists.",
        ) from exc
    write_audit(
        db,
        user=current_user,
        action="create",
        resource_type="ai_provider",
        resource_id=str(row.id),
        resource_display=row.name,
        new_value={
            **body.model_dump(mode="json", exclude={"api_key"}),
            "has_api_key": body.api_key is not None and body.api_key != "",
        },
    )
    await db.commit()
    await db.refresh(row)
    logger.info("ai_provider_created", provider_id=str(row.id), kind=row.kind)
    return _to_response(row)


@router.get("/providers/{provider_id}", response_model=ProviderResponse)
async def get_provider(
    provider_id: uuid.UUID, current_user: SuperAdmin, db: DB
) -> ProviderResponse:
    row = await db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return _to_response(row)


@router.put("/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: uuid.UUID,
    body: ProviderUpdate,
    current_user: SuperAdmin,
    db: DB,
) -> ProviderResponse:
    row = await db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        if k == "api_key":
            # None already excluded by exclude_unset; "" clears, anything else replaces.
            if v == "":
                row.api_key_encrypted = None
            else:
                row.api_key_encrypted = encrypt_str(v)
        else:
            setattr(row, k, v)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="name conflict") from exc
    write_audit(
        db,
        user=current_user,
        action="update",
        resource_type="ai_provider",
        resource_id=str(row.id),
        resource_display=row.name,
        new_value={k: ("***" if k == "api_key" else v) for k, v in changes.items()},
    )
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: uuid.UUID, current_user: SuperAdmin, db: DB) -> None:
    row = await db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    write_audit(
        db,
        user=current_user,
        action="delete",
        resource_type="ai_provider",
        resource_id=str(row.id),
        resource_display=row.name,
    )
    await db.delete(row)
    await db.commit()


@router.post(
    "/providers/test",
    response_model=TestConnectionResponse,
)
async def test_unsaved_provider(
    body: TestConnectionRequest, current_user: SuperAdmin
) -> TestConnectionResponse:
    """Probe a provider before persisting — lets the operator check
    config from the create modal.
    """
    if body.kind not in known_kinds():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"kind must be one of: {', '.join(known_kinds())}",
        )
    fake = _EphemeralProvider(
        kind=body.kind,
        base_url=body.base_url,
        api_key=body.api_key,
        options=body.options,
    )
    driver = get_driver(fake)
    # Run the probe with a hard cap — providers may hang on bad URLs.
    try:
        result = await asyncio.wait_for(driver.test_connection(), timeout=30.0)
    except TimeoutError:
        return TestConnectionResponse(
            ok=False,
            detail="probe timed out after 30s",
            latency_ms=30000,
            sample_models=[],
        )
    return TestConnectionResponse(
        ok=result.ok,
        detail=result.detail,
        latency_ms=result.latency_ms,
        sample_models=list(result.sample_models),
    )


@router.post(
    "/providers/{provider_id}/test",
    response_model=TestConnectionResponse,
)
async def test_saved_provider(
    provider_id: uuid.UUID, current_user: SuperAdmin, db: DB
) -> TestConnectionResponse:
    row = await db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    driver = get_driver(row)
    try:
        result = await asyncio.wait_for(driver.test_connection(), timeout=30.0)
    except TimeoutError:
        return TestConnectionResponse(
            ok=False,
            detail="probe timed out after 30s",
            latency_ms=30000,
            sample_models=[],
        )
    return TestConnectionResponse(
        ok=result.ok,
        detail=result.detail,
        latency_ms=result.latency_ms,
        sample_models=list(result.sample_models),
    )


@router.get(
    "/providers/{provider_id}/models",
    response_model=ListModelsResponse,
)
async def list_models(
    provider_id: uuid.UUID, current_user: SuperAdmin, db: DB
) -> ListModelsResponse:
    """Return the full model list for the provider — used by the
    UI's model picker, which needs more than the 10-sample limit
    of the test-connection response.
    """
    row = await db.get(AIProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    driver = get_driver(row)
    try:
        models = await asyncio.wait_for(driver.list_models(), timeout=30.0)
    except TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="provider did not respond within 30s",
        ) from None
    return ListModelsResponse(
        models=[
            {"id": m.id, "owned_by": m.owned_by, "context_window": m.context_window} for m in models
        ]
    )
