"""Curated VoIP DHCP-option catalog (issue #112 phase 1).

Read-only endpoint backing the vendor-grouped option picker in the
phone-profile editor. Catalog is static JSON shipped with the app
(``app/data/dhcp_voip_options.json``).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.services.dhcp.voip_options import VoIPVendor, load_catalog

router = APIRouter(tags=["dhcp"])


class VoIPOptionDef(BaseModel):
    code: int
    name: str
    kind: str
    use: str


class VoIPVendorDef(BaseModel):
    vendor: str
    match_hint: str
    description: str
    options: list[VoIPOptionDef]


def _to_response(v: VoIPVendor) -> VoIPVendorDef:
    return VoIPVendorDef(
        vendor=v.vendor,
        match_hint=v.match_hint,
        description=v.description,
        options=[
            VoIPOptionDef(code=o.code, name=o.name, kind=o.kind, use=o.use) for o in v.options
        ],
    )


@router.get("/voip-options", response_model=list[VoIPVendorDef])
async def list_voip_vendors(user: CurrentUser) -> list[VoIPVendorDef]:
    """List every curated VoIP vendor recipe.

    Returned in alphabetical order by vendor name. Each entry carries
    a ``match_hint`` (the option-60 vendor-class-id substring most
    commonly used to fence the phone profile) and an option list with
    the recommended DHCP options for that vendor.
    """
    _ = user
    return [_to_response(v) for v in load_catalog()]
