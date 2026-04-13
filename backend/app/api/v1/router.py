from fastapi import APIRouter

from app.api.v1.auth.router import router as auth_router
from app.api.v1.ipam.router import router as ipam_router

api_v1_router = APIRouter()

api_v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_v1_router.include_router(ipam_router, prefix="/ipam", tags=["ipam"])
