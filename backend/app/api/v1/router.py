from fastapi import APIRouter

from app.api.v1.audit.router import router as audit_router
from app.api.v1.auth.router import router as auth_router
from app.api.v1.auth_providers.router import router as auth_providers_router
from app.api.v1.custom_fields.router import router as custom_fields_router
from app.api.v1.dhcp import router as dhcp_router
from app.api.v1.dns.agents import router as dns_agents_router
from app.api.v1.dns.blocklist_router import router as dns_blocklist_router
from app.api.v1.dns.router import router as dns_router
from app.api.v1.groups.router import router as groups_router
from app.api.v1.ipam.router import router as ipam_router
from app.api.v1.roles.router import router as roles_router
from app.api.v1.search.router import router as search_router
from app.api.v1.settings.router import router as settings_router
from app.api.v1.users.router import router as users_router
from app.api.v1.vlans.router import router as vlans_router

api_v1_router = APIRouter()

api_v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_v1_router.include_router(
    auth_providers_router, prefix="/auth-providers", tags=["auth-providers"]
)
api_v1_router.include_router(ipam_router, prefix="/ipam", tags=["ipam"])
api_v1_router.include_router(dns_router, prefix="/dns", tags=["dns"])
api_v1_router.include_router(dns_blocklist_router, prefix="/dns", tags=["dns-blocklists"])
api_v1_router.include_router(dns_agents_router, prefix="/dns", tags=["dns-agents"])
api_v1_router.include_router(dhcp_router, prefix="/dhcp", tags=["dhcp"])
api_v1_router.include_router(users_router, prefix="/users", tags=["users"])
api_v1_router.include_router(groups_router, prefix="/groups", tags=["groups"])
api_v1_router.include_router(roles_router, prefix="/roles", tags=["roles"])
api_v1_router.include_router(audit_router, prefix="/audit", tags=["audit"])
api_v1_router.include_router(search_router, prefix="/search", tags=["search"])
api_v1_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_v1_router.include_router(custom_fields_router, prefix="/custom-fields", tags=["custom-fields"])
api_v1_router.include_router(vlans_router, prefix="/vlans", tags=["vlans"])
