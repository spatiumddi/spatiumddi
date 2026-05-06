from fastapi import APIRouter, Depends

from app.api.v1.acme import router as acme_router
from app.api.v1.admin.containers import router as containers_router
from app.api.v1.admin.feature_modules import router as feature_modules_router
from app.api.v1.admin.postgres import router as postgres_router
from app.api.v1.admin.trash import router as trash_router
from app.api.v1.ai import router as ai_router
from app.api.v1.alerts.router import router as alerts_router
from app.api.v1.api_tokens.router import router as api_tokens_router
from app.api.v1.applications import router as applications_router
from app.api.v1.asns import router as asns_router
from app.api.v1.audit.router import router as audit_router
from app.api.v1.auth.router import router as auth_router
from app.api.v1.auth_providers.router import router as auth_providers_router
from app.api.v1.circuits import router as circuits_router
from app.api.v1.conformity import router as conformity_router
from app.api.v1.custom_fields.router import router as custom_fields_router
from app.api.v1.dashboards import router as dashboards_router
from app.api.v1.dhcp import router as dhcp_router
from app.api.v1.dns.agents import router as dns_agents_router
from app.api.v1.dns.blocklist_router import router as dns_blocklist_router
from app.api.v1.dns.pool_router import router as dns_pool_router
from app.api.v1.dns.router import router as dns_router
from app.api.v1.dns_tools import router as dns_tools_router
from app.api.v1.docker import router as docker_router
from app.api.v1.domains import router as domains_router
from app.api.v1.groups.router import router as groups_router
from app.api.v1.ipam.router import router as ipam_router
from app.api.v1.kubernetes import router as kubernetes_router
from app.api.v1.logs import logs_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.network import router as network_router
from app.api.v1.nmap import router as nmap_router
from app.api.v1.overlays import router as overlays_router
from app.api.v1.ownership import (
    customers_router,
    providers_router,
    sites_router,
)
from app.api.v1.proxmox import router as proxmox_router
from app.api.v1.roles.router import router as roles_router
from app.api.v1.search.router import router as search_router
from app.api.v1.services import router as services_router
from app.api.v1.sessions.router import router as sessions_router
from app.api.v1.settings.router import router as settings_router
from app.api.v1.tags import router as tags_router
from app.api.v1.tailscale import router as tailscale_router
from app.api.v1.users.router import router as users_router
from app.api.v1.version import router as version_router
from app.api.v1.vlans.router import router as vlans_router
from app.api.v1.vrfs import router as vrfs_router
from app.api.v1.webhooks import router as webhooks_router
from app.services.feature_modules import require_module

api_v1_router = APIRouter()

# Tags are alphabetised so the ReDoc / Swagger surface lists sections
# A → Z. New entries should be inserted in sort order.
api_v1_router.include_router(acme_router, prefix="/acme", tags=["acme"])
api_v1_router.include_router(
    ai_router,
    prefix="/ai",
    tags=["ai"],
    dependencies=[Depends(require_module("ai.copilot"))],
)
api_v1_router.include_router(containers_router, prefix="/admin", tags=["admin-containers"])
api_v1_router.include_router(
    feature_modules_router, prefix="/admin", tags=["admin-feature-modules"]
)
api_v1_router.include_router(postgres_router, prefix="/admin", tags=["admin-postgres"])
api_v1_router.include_router(trash_router, prefix="/admin", tags=["admin-trash"])
api_v1_router.include_router(alerts_router, prefix="/alerts", tags=["alerts"])
api_v1_router.include_router(api_tokens_router, prefix="/api-tokens", tags=["api-tokens"])
api_v1_router.include_router(applications_router, prefix="/applications", tags=["applications"])
api_v1_router.include_router(
    asns_router,
    prefix="/asns",
    tags=["asns"],
    dependencies=[Depends(require_module("network.asn"))],
)
api_v1_router.include_router(audit_router, prefix="/audit", tags=["audit"])
api_v1_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_v1_router.include_router(
    auth_providers_router, prefix="/auth-providers", tags=["auth-providers"]
)
api_v1_router.include_router(
    circuits_router,
    prefix="/circuits",
    tags=["circuits"],
    dependencies=[Depends(require_module("network.circuit"))],
)
api_v1_router.include_router(
    conformity_router,
    prefix="/conformity",
    tags=["conformity"],
    dependencies=[Depends(require_module("compliance.conformity"))],
)
api_v1_router.include_router(custom_fields_router, prefix="/custom-fields", tags=["custom-fields"])
api_v1_router.include_router(dashboards_router, prefix="/dashboards", tags=["dashboards"])
api_v1_router.include_router(
    customers_router,
    prefix="/customers",
    tags=["customers"],
    dependencies=[Depends(require_module("network.customer"))],
)
api_v1_router.include_router(dhcp_router, prefix="/dhcp", tags=["dhcp"])
api_v1_router.include_router(dns_router, prefix="/dns", tags=["dns"])
api_v1_router.include_router(dns_agents_router, prefix="/dns", tags=["dns-agents"])
api_v1_router.include_router(dns_blocklist_router, prefix="/dns", tags=["dns-blocklists"])
api_v1_router.include_router(dns_pool_router, prefix="/dns", tags=["dns-pools"])
api_v1_router.include_router(dns_tools_router, prefix="/dns", tags=["dns-tools"])
api_v1_router.include_router(
    docker_router,
    prefix="/docker",
    tags=["docker"],
    dependencies=[Depends(require_module("integrations.docker"))],
)
api_v1_router.include_router(domains_router, tags=["domains"])
api_v1_router.include_router(groups_router, prefix="/groups", tags=["groups"])
api_v1_router.include_router(ipam_router, prefix="/ipam", tags=["ipam"])
api_v1_router.include_router(
    kubernetes_router,
    prefix="/kubernetes",
    tags=["kubernetes"],
    dependencies=[Depends(require_module("integrations.kubernetes"))],
)
api_v1_router.include_router(logs_router, prefix="/logs", tags=["logs"])
api_v1_router.include_router(metrics_router, prefix="/metrics", tags=["metrics"])
api_v1_router.include_router(
    network_router,
    tags=["network"],
    dependencies=[Depends(require_module("network.device"))],
)
api_v1_router.include_router(
    nmap_router,
    prefix="/nmap",
    tags=["nmap"],
    dependencies=[Depends(require_module("tools.nmap"))],
)
api_v1_router.include_router(
    overlays_router,
    prefix="/overlays",
    tags=["overlays"],
    dependencies=[Depends(require_module("network.overlay"))],
)
api_v1_router.include_router(
    providers_router,
    prefix="/providers",
    tags=["providers"],
    dependencies=[Depends(require_module("network.provider"))],
)
api_v1_router.include_router(
    proxmox_router,
    prefix="/proxmox",
    tags=["proxmox"],
    dependencies=[Depends(require_module("integrations.proxmox"))],
)
api_v1_router.include_router(roles_router, prefix="/roles", tags=["roles"])
api_v1_router.include_router(search_router, prefix="/search", tags=["search"])
api_v1_router.include_router(
    services_router,
    prefix="/services",
    tags=["services"],
    dependencies=[Depends(require_module("network.service"))],
)
api_v1_router.include_router(sessions_router, prefix="/sessions", tags=["sessions"])
api_v1_router.include_router(settings_router, prefix="/settings", tags=["settings"])
api_v1_router.include_router(
    sites_router,
    prefix="/sites",
    tags=["sites"],
    dependencies=[Depends(require_module("network.site"))],
)
api_v1_router.include_router(tags_router, prefix="/tags", tags=["tags"])
api_v1_router.include_router(
    tailscale_router,
    prefix="/tailscale",
    tags=["tailscale"],
    dependencies=[Depends(require_module("integrations.tailscale"))],
)
api_v1_router.include_router(users_router, prefix="/users", tags=["users"])
api_v1_router.include_router(version_router, prefix="/version", tags=["version"])
api_v1_router.include_router(
    vlans_router,
    prefix="/vlans",
    tags=["vlans"],
    dependencies=[Depends(require_module("network.vlan"))],
)
api_v1_router.include_router(
    vrfs_router,
    prefix="/vrfs",
    tags=["vrfs"],
    dependencies=[Depends(require_module("network.vrf"))],
)
api_v1_router.include_router(webhooks_router, prefix="/webhooks", tags=["webhooks"])
