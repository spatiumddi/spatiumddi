from fastapi import APIRouter

from app.api.v1.acme import router as acme_router
from app.api.v1.admin.containers import router as containers_router
from app.api.v1.admin.postgres import router as postgres_router
from app.api.v1.admin.trash import router as trash_router
from app.api.v1.alerts.router import router as alerts_router
from app.api.v1.api_tokens.router import router as api_tokens_router
from app.api.v1.asns import router as asns_router
from app.api.v1.audit.router import router as audit_router
from app.api.v1.auth.router import router as auth_router
from app.api.v1.auth_providers.router import router as auth_providers_router
from app.api.v1.custom_fields.router import router as custom_fields_router
from app.api.v1.dhcp import router as dhcp_router
from app.api.v1.dns.agents import router as dns_agents_router
from app.api.v1.dns.blocklist_router import router as dns_blocklist_router
from app.api.v1.dns.pool_router import router as dns_pool_router
from app.api.v1.dns.router import router as dns_router
from app.api.v1.dns_tools import router as dns_tools_router
from app.api.v1.docker import router as docker_router
from app.api.v1.groups.router import router as groups_router
from app.api.v1.ipam.router import router as ipam_router
from app.api.v1.kubernetes import router as kubernetes_router
from app.api.v1.logs import logs_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.network import router as network_router
from app.api.v1.nmap import router as nmap_router
from app.api.v1.proxmox import router as proxmox_router
from app.api.v1.roles.router import router as roles_router
from app.api.v1.search.router import router as search_router
from app.api.v1.settings.router import router as settings_router
from app.api.v1.tailscale import router as tailscale_router
from app.api.v1.users.router import router as users_router
from app.api.v1.version import router as version_router
from app.api.v1.vlans.router import router as vlans_router
from app.api.v1.webhooks import router as webhooks_router

api_v1_router = APIRouter()

api_v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_v1_router.include_router(
    auth_providers_router, prefix="/auth-providers", tags=["auth-providers"]
)
api_v1_router.include_router(api_tokens_router, prefix="/api-tokens", tags=["api-tokens"])
api_v1_router.include_router(ipam_router, prefix="/ipam", tags=["ipam"])
api_v1_router.include_router(dns_router, prefix="/dns", tags=["dns"])
api_v1_router.include_router(dns_tools_router, prefix="/dns", tags=["dns-tools"])
api_v1_router.include_router(dns_blocklist_router, prefix="/dns", tags=["dns-blocklists"])
api_v1_router.include_router(dns_pool_router, prefix="/dns", tags=["dns-pools"])
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
api_v1_router.include_router(logs_router, prefix="/logs", tags=["logs"])
api_v1_router.include_router(alerts_router, prefix="/alerts", tags=["alerts"])
api_v1_router.include_router(asns_router, prefix="/asns", tags=["asns"])
api_v1_router.include_router(acme_router, prefix="/acme", tags=["acme"])
api_v1_router.include_router(metrics_router, prefix="/metrics", tags=["metrics"])
api_v1_router.include_router(version_router, prefix="/version", tags=["version"])
api_v1_router.include_router(kubernetes_router, prefix="/kubernetes", tags=["kubernetes"])
api_v1_router.include_router(docker_router, prefix="/docker", tags=["docker"])
api_v1_router.include_router(proxmox_router, prefix="/proxmox", tags=["proxmox"])
api_v1_router.include_router(tailscale_router, prefix="/tailscale", tags=["tailscale"])
api_v1_router.include_router(network_router, tags=["network"])
api_v1_router.include_router(nmap_router, prefix="/nmap", tags=["nmap"])
api_v1_router.include_router(trash_router, prefix="/admin", tags=["admin-trash"])
api_v1_router.include_router(postgres_router, prefix="/admin", tags=["admin-postgres"])
api_v1_router.include_router(containers_router, prefix="/admin", tags=["admin-containers"])
api_v1_router.include_router(webhooks_router, prefix="/webhooks", tags=["webhooks"])
