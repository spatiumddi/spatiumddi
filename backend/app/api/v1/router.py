from fastapi import APIRouter

from app.api.v1.audit.router import router as audit_router
from app.api.v1.auth.router import router as auth_router
from app.api.v1.custom_fields.router import router as custom_fields_router
from app.api.v1.dns.blocklist_router import router as dns_blocklist_router
from app.api.v1.dns.router import router as dns_router
from app.api.v1.ipam.router import router as ipam_router
from app.api.v1.search.router import router as search_router
from app.api.v1.settings.router import router as settings_router
from app.api.v1.users.router import router as users_router

api_v1_router = APIRouter()

api_v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_v1_router.include_router(ipam_router, prefix="/ipam", tags=["ipam"])
api_v1_router.include_router(dns_router, prefix="/dns", tags=["dns"])
api_v1_router.include_router(dns_blocklist_router, prefix="/dns", tags=["dns-blocklists"])
api_v1_router.include_router(users_router, prefix="/users", tags=["users"])
api_v1_router.include_router(audit_router, prefix="/audit", tags=["audit"])
api_v1_router.include_router(search_router, prefix="/search", tags=["search"])
api_v1_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_v1_router.include_router(custom_fields_router, prefix="/custom-fields", tags=["custom-fields"])
