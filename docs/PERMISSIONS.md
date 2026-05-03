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
| `settings`        | Platform settings                                     |
| `api_token`       | API tokens                                            |
| `acme_account`    | ACME DNS-01 provider credentials (`/api/v1/acme/`)    |
| `*`               | Wildcard — match any resource type                    |

## Evaluation rules

1. **Superadmin short-circuits everything.** If `User.is_superadmin=True`
   the check always passes (no audit denial written).
2. **Inactive users are denied** regardless of permissions.
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
| `IPAM Editor`  | `admin` on `ip_space`, `ip_block`, `subnet`, `ip_address`, `vlan`, `custom_field` |
| `DNS Editor`   | `admin` on `dns_zone`, `dns_record`, `dns_group`, `dns_blocklist` |
| `DHCP Editor`  | `admin` on `dhcp_server`, `dhcp_scope`, `dhcp_pool`, `dhcp_static`, `dhcp_client_class` |

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
