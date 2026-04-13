from fastapi import APIRouter

from app.api.v1.audit.router import router as audit_router
from app.api.v1.auth.router import router as auth_router
from app.api.v1.ipam.router import router as ipam_router
from app.api.v1.users.router import router as users_router

api_v1_router = APIRouter()

api_v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_v1_router.include_router(ipam_router, prefix="/ipam", tags=["ipam"])
api_v1_router.include_router(users_router, prefix="/users", tags=["users"])
api_v1_router.include_router(audit_router, prefix="/audit", tags=["audit"])
