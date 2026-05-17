# Permissions Model

SpatiumDDI uses a **group-based RBAC** model. Users are members of one or more
**Groups**, groups carry one or more **Roles**, and roles carry a list of
**Permission entries**. Every API mutation is checked server-side (see
non-negotiable #3 in `CLAUDE.md`).

## The Permission Entry

Each entry in `Role.permissions` (JSONB) is an object with this shape:

```json
{
  "action": "write",
  "resource_type": "subnet",
  "resource_id": "c3f1e7b9-2a5d-…"
}
```

| Field           | Required | Meaning                                                                |
| --------------- | -------- | ---------------------------------------------------------------------- |
| `action`        | yes      | What the user may do. See **Actions** below.                           |
| `resource_type` | yes      | Which resource kind. See **Resource types** below.                     |
| `resource_id`   | no       | Scope to a specific UUID. Omit / `null` for "any instance of the type". |

### Actions

| Action   | Meaning                                                    |
| -------- | ---------------------------------------------------------- |
| `read`   | GET / list / export                                        |
| `write`  | POST / PUT / PATCH (create, update)                        |
| `delete` | DELETE                                                     |
| `admin`  | All of `read`, `write`, `delete` for the given resource    |
| `*`      | Wildcard — match any action                                |

### Resource types

| `resource_type`   | Covers                                                |
| ----------------- | ----------------------------------------------------- |
| `ip_space`        | IPAM spaces                                           |
| `ip_block`        | Top-level CIDR blocks                                 |
| `subnet`          | Subnets under a block                                 |
| `ip_address`      | Individual IPs, aliases, bulk address ops             |
| `vlan`            | VLANs and routers                                     |
| `vrf`             | VRFs (Virtual Routing and Forwarding) — `manage_vrfs` |
| `asn`             | Autonomous Systems + RPKI ROAs + BGP peering / communities — `manage_asns` |
| `domain`          | Domain registration tracking (registrar / expiry / NS drift) — `manage_domains` |
| `dns_zone`        | DNS zones (forward and reverse)                       |
| `dns_record`      | DNS records within a zone                             |
| `dns_group`       | DNS server groups, servers, views, ACLs, trust anchors |
| `dns_blocklist`   | Response Policy Zones / blocking lists                |
| `dhcp_server`     | Kea + Windows DHCP server objects and server groups   |
| `dhcp_scope`      | DHCP scopes / shared networks                         |
| `dhcp_pool`       | Address pools within a scope                          |
| `dhcp_static`     | Static / reserved leases                              |
| `dhcp_client_class` | DHCP client classes                                 |
| `audit_log`       | Audit log read                                        |
| `user`            | Users — **superadmin only** in practice               |
| `group`           | Groups — admin required                               |
| `role`            | Roles — admin required                                |
| `auth_provider`   | LDAP/OIDC/SAML providers — superadmin only            |
| `custom_field`    | Custom field definitions                              |
| `manage_ipam_templates` | IPAM template classes (#26)                     |
| `settings`        | Platform settings                                     |
| `api_token`       | API tokens                                            |
| `acme_account`    | ACME DNS-01 provider credentials (`/api/v1/acme/`)    |
| `customer`        | Customer logical-ownership entity (#91)               |
| `site`            | Site logical-ownership entity (#91)                   |
| `provider`        | Provider logical-ownership entity (#91)               |
| `circuit`         | WAN circuit (#93)                                     |
| `network_service` | Service-catalog row (#94) — bundles VRF/Subnet/IPBlock/DNSZone/DHCPScope/Circuit/Site/OverlayNetwork into a customer deliverable |
| `overlay_network` | SD-WAN overlay topology + sites (#95)                 |
| `routing_policy`  | Per-overlay declarative routing policies (#95)        |
| `application_category` | SaaS application catalog used by `match_kind=application` (#95) |
| `conformity`      | Conformity policies + results + auditor PDF export (#106) |
| `*`               | Wildcard — match any resource type                    |

## Evaluation rules

1. **Superadmin short-circuits everything.** If `User.is_superadmin=True`
   the check always passes (no audit denial written).
2. **Inactive users are denied** regardless of permissions.

### "Effective superadmin" — legacy flag + RBAC wildcard

Two paths grant superadmin-level access (#190):

- **Legacy column** — `User.is_superadmin=True` set directly on the
  row (seeded `admin`, anyone explicitly flagged in the user-admin
  form).
- **RBAC wildcard** — the user is a member of a group whose role
  carries a `{action: "*", resource_type: "*"}` permission (the
  built-in `Superadmin` role or any clone of it).

Both pass `require_permission` gates identically. For endpoints with
`Depends(require_superadmin)` (or an inline `is_effective_superadmin`
check), both also admit — without this unification, users provisioned
via LDAP / OIDC / SAML and mapped into a Superadmin-role group passed
every `require_permission` check but 403'd on hand-rolled superadmin
helpers (the canonical pre-#190 bug).

Carve-out: the legacy-flag path keeps admitting **inactive**
superadmins so a disabled bootstrap admin can still reach diagnostic
surfaces during incident triage. The wildcard-permission path still
requires `is_active=True` because `user_has_permission` short-circuits
on inactive.
3. **Match algorithm** for a given check (`action`, `resource_type`, `resource_id`):
   - Walk every role in every group the user is a member of.
   - A permission entry matches when:
     - `entry.action == "*"` or `entry.action == action` or
       (`entry.action == "admin"` and `action ∈ {read, write, delete, admin}`), AND
     - `entry.resource_type == "*"` or `entry.resource_type == resource_type`, AND
     - `entry.resource_id` is missing/empty/`"*"`, OR `entry.resource_id == resource_id`.
   - Any single matching entry grants the permission.
4. **Unscoped vs scoped checks.** A permission with `resource_id` set cannot
   satisfy an unscoped check (one passed without `resource_id`). This prevents
   "I have write on *this one* subnet" from accidentally granting "write on
   subnets in general".

## Built-in roles (seeded on first start)

| Role           | Permissions                                                  |
| -------------- | ------------------------------------------------------------ |
| `Superadmin`   | `[{"action": "*", "resource_type": "*"}]`                    |
| `Viewer`       | `[{"action": "read", "resource_type": "*"}]`                 |
| `IPAM Editor`  | `admin` on `ip_space`, `ip_block`, `subnet`, `ip_address`, `vlan`, `nat_mapping`, `custom_field`, `manage_ipam_templates`, `customer`, `site`, `provider`, `network_service` |
| `DNS Editor`   | `admin` on `dns_zone`, `dns_record`, `dns_group`, `dns_blocklist`, `manage_dns_pools` |
| `DHCP Editor`  | `admin` on `dhcp_server`, `dhcp_scope`, `dhcp_pool`, `dhcp_static`, `dhcp_client_class`, `dhcp_option_template`, `dhcp_mac_block` |
| `Network Editor` | `admin` on `manage_network_devices`, `manage_nmap_scans`, `manage_asns`, `vrf`, `circuit`, `network_service`, `overlay_network`, `routing_policy`, `application_category`, `customer`, `site`, `provider` |
| `Auditor`        | `read` on `conformity`, `audit`, `subnet`, `ip_address`, `dns_zone`, `dhcp_scope` — external auditor account, can view conformity dashboard + pull the auditor PDF + verify supporting evidence without making changes |
| `Compliance Editor` | `admin` on `conformity`, `read` on `audit`, `subnet`, `ip_address`, `dns_zone`, `dhcp_scope` — for the team that authors / tunes conformity policies without touching operational config |

Built-in roles (`is_builtin=True`) can be cloned but not deleted.

## Using the helpers

```python
from app.core.permissions import require_resource_permission, user_has_permission

# Router-level: GET=read, POST/PUT=write, DELETE=delete
router = APIRouter(dependencies=[Depends(require_resource_permission("subnet"))])

# Fine-grained inside a handler:
subnet = await db.get(Subnet, subnet_id)
if not user_has_permission(current_user, "write", "subnet", subnet.id):
    raise HTTPException(403, "Permission denied on this subnet")
```

See `backend/app/core/permissions.py` for the full API.
