"""DHCP option-code library lookup.

Read-only endpoint backing the autocomplete on the custom-options row
of ``DHCPOptionsEditor``. Catalog is static JSON shipped with the app
(``app/data/dhcp_option_codes.json``).
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.services.dhcp.option_codes import DHCPOptionCodeDef, search

router = APIRouter(tags=["dhcp"])


class OptionCodeDef(BaseModel):
    code: int
    name: str
    kind: str
    description: str
    rfc: str | None = None


def _to_response(d: DHCPOptionCodeDef) -> OptionCodeDef:
    return OptionCodeDef(
        code=d.code,
        name=d.name,
        kind=d.kind,
        description=d.description,
        rfc=d.rfc,
    )


@router.get("/option-codes", response_model=list[OptionCodeDef])
async def list_option_codes(
    user: CurrentUser,
    q: str | None = Query(
        default=None, description="Substring search on name/description; numeric prefix on code"
    ),
    limit: int = Query(default=200, ge=1, le=512),
) -> list[OptionCodeDef]:
    _ = user
    matches = search(q)
    return [_to_response(d) for d in matches[:limit]]
