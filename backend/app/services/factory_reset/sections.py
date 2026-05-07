"""Factory-reset section catalog (issue #116).

12 user-facing sections, mapped from the issue body. Each section
has:

* a stable ``key`` operators reference in the API + UI
* a human-readable ``label``
* a confirmation ``phrase`` the operator must re-type to commit
* a ``kind`` describing how the wipe runs:
  - ``truncate`` — straight ``TRUNCATE TABLE … CASCADE`` over the
    declared tables. The bulk of sections.
  - ``auth_rbac`` — partial wipe that preserves the calling user's
    row, group memberships, role assignments, and the platform
    builtin roles.
  - ``settings_reset`` — UPDATE the singleton ``platform_settings``
    row to its defaults. Doesn't touch any other table.
* a ``tables`` list (for ``truncate`` kind) — what gets wiped.

Sections deliberately overlap in some places — e.g. DNS query log
is in both DNS and Observability logs. That's intentional: an
operator wiping DNS expects the query log to go too; an operator
wiping observability expects DNS data to stay. TRUNCATE is
idempotent so the overlap is safe.

Tables NOT mentioned in any section are intentionally untouchable
by factory reset:

* ``alembic_version`` / ``oui_vendor`` — schema head + IEEE OUI
  cache. Wiping these would render the install unusable.
* ``backup_target`` — the backup-destination configs are the
  safety net. Wiping them mid-reset removes the restore path.
* ``feature_module`` — module enable/disable flags persist across
  resets so operators don't have to re-enable everything.
* ``event_outbox`` — in-flight webhook deliveries, not user data.
* ``internal_error`` — captured exception traces are diagnostic
  state, not configuration.
* ``audit_forward_target`` — keep operating the syslog / webhook /
  chat audit forwarders even when audit_log itself is wiped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# The "Everything" target is implemented as a synthetic section that
# expands to every other section's tables — see ALL_SECTION_KEY.
ALL_SECTION_KEY = "all"


@dataclass(frozen=True)
class FactorySection:
    key: str
    label: str
    description: str
    phrase: str
    kind: Literal["truncate", "auth_rbac", "settings_reset", "everything"]
    tables: tuple[str, ...] = field(default_factory=tuple)


# Sections in the order they'll render on the UI page. Sequence
# follows the issue body exactly.
FACTORY_SECTIONS: tuple[FactorySection, ...] = (
    FactorySection(
        key="ipam",
        label="IPAM",
        description=(
            "IP spaces, blocks, subnets, IP addresses, NAT mappings, "
            "VLANs, VRFs, ASNs, IPAM templates, custom-field "
            "definitions. Cascading FKs sweep dependent DNS-record "
            "↔ IP and DHCP-static ↔ IP links to NULL."
        ),
        phrase="DESTROY-IPAM",
        kind="truncate",
        tables=(
            "ip_address",
            "subnet_plan",
            "subnet",
            "ip_block",
            "ip_space",
            "nat_mapping",
            "vlan_mapping",
            "vlan",
            "vrf",
            "asn_rpki_roa",
            "bgp_peering",
            "bgp_community",
            "asn",
            "ipam_template",
            "custom_field_definition",
            "ip_mac_history",
        ),
    ),
    FactorySection(
        key="dns",
        label="DNS",
        description=(
            "DNS server groups, servers, zones, records, blocking "
            "lists, GSLB pools, TSIG keys, ACLs, DNS query log, "
            "trust anchors. The dns_record_op queue is also flushed."
        ),
        phrase="DESTROY-DNS",
        kind="truncate",
        tables=(
            "dns_record",
            "dns_record_op",
            "dns_pool_member",
            "dns_pool",
            "dns_blocklist_view_assoc",
            "dns_blocklist_group_assoc",
            "dns_blocklist_exception",
            "dns_blocklist_entry",
            "dns_blocklist",
            "dns_view",
            "dns_acl_entry",
            "dns_acl",
            "dns_zone",
            "dns_server_options",
            "dns_server_zone_state",
            "dns_server_runtime_state",
            "dns_server",
            "dns_server_group",
            "dns_tsig_key",
            "dns_trust_anchor",
            "dns_query_log_entry",
            "dns_metric_sample",
            "subnet_domain",
            "domain",
        ),
    ),
    FactorySection(
        key="dhcp",
        label="DHCP",
        description=(
            "DHCP server groups, servers, scopes, pools, static "
            "assignments, client classes, option templates, MAC "
            "blocks, PXE profiles, leases + lease history, the "
            "config-ops queue."
        ),
        phrase="DESTROY-DHCP",
        kind="truncate",
        tables=(
            "dhcp_lease_history",
            "dhcp_lease",
            "dhcp_static_assignment",
            "dhcp_pool",
            "dhcp_scope",
            "dhcp_phone_profile_scope",
            "dhcp_phone_profile",
            "dhcp_pxe_arch_match",
            "dhcp_pxe_profile",
            "dhcp_mac_block",
            "dhcp_option_template",
            "dhcp_client_class",
            "dhcp_server",
            "dhcp_server_group",
            "dhcp_log_entry",
            "dhcp_metric_sample",
            "dhcp_config_op",
            "dhcp_fingerprint",
        ),
    ),
    FactorySection(
        key="network_modeling",
        label="Network modeling",
        description=(
            "Customers, sites, providers, services, circuits, "
            "overlays, routing policies, application catalog, "
            "network-service ↔ resource bindings."
        ),
        phrase="DESTROY-NETWORK-MODELING",
        kind="truncate",
        tables=(
            "network_service_resource",
            "network_service",
            "routing_policy",
            "overlay_site",
            "overlay_network",
            "circuit",
            "site",
            "customer",
            "provider",
            "application_category",
        ),
    ),
    FactorySection(
        key="integrations",
        label="Integrations",
        description=(
            "Kubernetes / Docker / Proxmox / Tailscale / UniFi "
            "endpoints + their mirrored network discovery rows "
            "(ARP, FDB, neighbours, interfaces, routers, ACME "
            "accounts). FK cascade clears any IPAM rows still "
            "owned by an integration."
        ),
        phrase="DESTROY-INTEGRATIONS",
        kind="truncate",
        tables=(
            "kubernetes_cluster",
            "docker_host",
            "proxmox_node",
            "tailscale_tenant",
            "unifi_controller",
            "network_arp_entry",
            "network_fdb_entry",
            "network_neighbour",
            "network_interface",
            "network_device",
            "router_zone",
            "router",
            "acme_account",
        ),
    ),
    FactorySection(
        key="ai",
        label="AI / Copilot",
        description=(
            "AI providers, custom prompts, chat sessions + " "messages, AI operation proposals."
        ),
        phrase="DESTROY-AI",
        kind="truncate",
        tables=(
            "ai_chat_message",
            "ai_chat_session",
            "ai_operation_proposal",
            "ai_prompt",
            "ai_provider",
        ),
    ),
    FactorySection(
        key="compliance",
        label="Compliance",
        description=(
            "Conformity policies, conformity results, alert rules, "
            "alert events. Resets compliance reporting back to a "
            "clean slate; built-in conformity policies re-seed at "
            "next startup."
        ),
        phrase="DESTROY-COMPLIANCE",
        kind="truncate",
        tables=(
            "conformity_result",
            "conformity_policy",
            "alert_event",
            "alert_rule",
        ),
    ),
    FactorySection(
        key="tools",
        label="Tools",
        description=(
            "nmap scan history + per-IP fingerprint state. The "
            "DHCP-fingerprint catalog (separate from device "
            "profiles) lives under DHCP."
        ),
        phrase="DESTROY-TOOLS",
        kind="truncate",
        tables=("nmap_scan",),
    ),
    FactorySection(
        key="observability_logs",
        label="Observability logs",
        description=(
            "Audit log + DNS query log + DHCP activity log. "
            "Separate from audit-forward targets so operators can "
            "wipe the diagnostic logs while keeping the syslog / "
            "webhook forwarders intact. A synthetic "
            "factory_reset_performed audit row is written AFTER "
            "the wipe so the trail of evidence survives."
        ),
        phrase="DESTROY-OBSERVABILITY-LOGS",
        kind="truncate",
        tables=(
            "audit_log",
            "dns_query_log_entry",
            "dhcp_log_entry",
            "internal_error",
        ),
    ),
    FactorySection(
        key="auth_rbac",
        label="Auth + RBAC",
        description=(
            "All non-admin users, all custom groups, all "
            "non-builtin roles, all API tokens, all auth providers. "
            "The calling superadmin and built-in roles "
            "(Superadmin / Viewer / IPAM/DNS/DHCP Editor / Auditor / "
            "Compliance Editor) are preserved automatically."
        ),
        phrase="DESTROY-AUTH-RBAC",
        kind="auth_rbac",
    ),
    FactorySection(
        key="settings_branding",
        label="Settings + branding",
        description=(
            "Reverts platform_settings to defaults. Keeps the "
            "encryption key intact (SECRET_KEY lives in env, not "
            "the DB). Audit-forward targets, event subscriptions, "
            "and feature-module toggles are not touched."
        ),
        phrase="DESTROY-SETTINGS",
        kind="settings_reset",
    ),
    FactorySection(
        key=ALL_SECTION_KEY,
        label="Everything",
        description=(
            "Run every section above sequentially. The calling "
            "superadmin, built-in roles, alembic head, OUI cache, "
            "configured backup destinations, and audit-forward "
            "targets are still preserved. Use this as the "
            "fresh-install button."
        ),
        phrase="FACTORY-RESET-ALL",
        kind="everything",
    ),
)


FACTORY_SECTIONS_BY_KEY: dict[str, FactorySection] = {s.key: s for s in FACTORY_SECTIONS}


def expand_everything() -> list[FactorySection]:
    """Return the ordered list of non-``everything`` sections that
    the ``all`` target expands to. Excludes the ``ALL_SECTION_KEY``
    pseudo-section itself.
    """
    return [s for s in FACTORY_SECTIONS if s.key != ALL_SECTION_KEY]
