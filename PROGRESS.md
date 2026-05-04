# Branch progress: `feat/ipam-templates`

Single-issue branch for #26 — IPAM template classes (reusable
stamp templates that pre-fill tags, custom fields, DNS / DHCP
group assignments, and optional sub-subnet layouts on create).

## Resume protocol

1. `git checkout feat/ipam-templates`
2. Read this file from top to bottom.
3. The first unchecked box under "Checkpoints" is the next thing
   to do.
4. This is a sizeable feature — split into commits at the
   model / service / API / UI seams; never squash mid-implementation.
5. When all "Done when" criteria check, run `make ci`, commit
   chain, merge to `main`.

## Issue #26 — IPAM template classes

### Spec (from issue)

> Reusable stamp templates that carry default tags, custom-field
> values, DNS / DHCP group assignments, and optional sub-subnet
> layouts. Applied to a block or subnet on create; existing
> instances can re-apply to pick up template drift. Phase 5 —
> belongs alongside advanced reporting / multi-tenancy, once the
> base inheritance story is fully bedded down.

### Implementation plan

- **Data model.** New `ipam_template` table:
  - `id: uuid PK`
  - `name: str unique`
  - `description: str | null`
  - `applies_to: enum("block", "subnet")` — a template either
    stamps blocks or subnets, not both. Two stamps with the same
    semantics for two types creates ambiguity at apply time.
  - `tags: dict` — JSONB key/value default tags.
  - `custom_fields: dict` — JSONB; values stamped onto the
    target's `custom_fields` column.
  - `dns_group_id: uuid | null` (FK SET NULL)
  - `dns_zone_id: uuid | null` (FK SET NULL) — optional
    primary zone pin
  - `dns_additional_zone_ids: list[uuid]` — optional pin
    of additional zones (already a column shape on Subnet)
  - `dhcp_group_id: uuid | null` (FK SET NULL)
  - `ddns_*` columns — same set Subnet carries for DDNS
    inheritance. Mirror the subnet model; null = inherit.
  - `child_layout: dict | null` — optional sub-subnet layout
    spec, only valid when `applies_to = "block"`. Schema:
    `{children: [{prefix: int, name_template: str,
    description?, tags?, custom_fields?}]}`. Children are
    carved with the standard split logic on apply.
  - `created_at` / `updated_at` standard.
- **Tracking back-references.** Add
  `ip_block.applied_template_id` and `subnet.applied_template_id`
  nullable FKs (SET NULL on template delete) so re-apply for
  drift can find every instance of a given template.
- **Schemas.** `IPAMTemplateCreate` / `IPAMTemplateUpdate` /
  `IPAMTemplateResponse`. Validators:
  - `child_layout` only allowed when `applies_to = "block"`.
  - Each child's `prefix` must be larger than the carrier's
    prefix (validation deferred to apply time since carrier's
    prefix isn't known until apply).
- **Service.** `app/services/ipam/templates.py`:
  - `apply_template_to_block(template_id, block_id)` — stamps
    tags / CFs / DDNS settings onto an existing block;
    optionally carves children per `child_layout`.
  - `apply_template_to_subnet(template_id, subnet_id)` — same
    minus child layout.
  - `apply_template_on_create_block(template_id, block_create)`
    — pre-fill helper used by `create_block`.
  - `apply_template_on_create_subnet(template_id, subnet_create)`
    — pre-fill helper used by `create_subnet`.
  - **Apply policy:** template values overwrite explicit
    operator-supplied values when `force=True`; otherwise
    templates fill ONLY null fields. Default `force=False`.
  - Re-apply for drift (`reapply_template_to_block(...)`) —
    same as apply but always with `force=True` because
    operator's intent is "match the template".
- **API.** Under `/api/v1/ipam/templates`:
  - `GET /` — list (with filters: `applies_to`, search).
  - `POST /` — create.
  - `GET /{id}` — get + `applied_to` count (count of
    `ip_block` + `subnet` rows pointing at it).
  - `PUT /{id}` — update.
  - `DELETE /{id}` — delete (cascade SET NULL via FK).
  - `POST /{id}/apply` — body `{block_id?: uuid, subnet_id?:
    uuid}` — apply to one target.
  - `POST /{id}/reapply-all` — apply to every recorded
    instance to refresh drift; capped at 200 / call; queued
    via Celery for big templates.
- **`create_block` / `create_subnet` integration.** Add
  `template_id` to `IPBlockCreate` / `SubnetCreate` schemas. If
  set, run the corresponding `apply_template_on_create_*`
  helper to merge template values BEFORE the row commits.
- **Frontend.** New page at `/admin/ipam/templates`:
  - List table: Name / Applies-to / DNS group / DHCP group /
    Children count / Applied-to count / Edit / Delete.
  - Editor modal: tabs for "General", "Tags + CFs", "DNS /
    DHCP", "Child layout" (only shown when applies_to=block).
  - Per-row "Reapply to all" button (with typed-name
    confirmation).
- **`AddBlockModal` / `AddSubnetModal` integration.** Optional
  "Apply template" combo at the top of the form. Picking one
  pre-fills the rest of the form client-side using the
  template's values; operator can still override before
  submit.
- **Permissions.** New `manage_ipam_templates` resource_type
  + grant in `IPAM Editor` builtin role.

### Checkpoints

- [x] Migration: `ipam_template` table + `applied_template_id` FKs on `ip_block` / `subnet` (`f9c1a7e25b83_ipam_template_classes`)
- [x] Models: `IPAMTemplate` + `applied_template_id` columns
- [x] Schemas: `IPAMTemplateCreate` / `Update` / `Response` + `template_id` on Create
- [x] Service: `app/services/ipam/templates.py` (apply / reapply / pre-fill helpers, `_prefill` uses Pydantic `model_fields_set` so booleans like `ddns_enabled=False` aren't mistaken for "operator-provided")
- [x] Router: `/api/v1/ipam/templates` CRUD + `/apply` + `/reapply-all` (cap 200/call)
- [x] Hook into `create_block` / `create_subnet` for `template_id` pre-fill + child_layout carve on block create
- [x] Permission: seed `manage_ipam_templates` into `IPAM Editor` (and into RolesPage resource-type list so admins can grant it standalone)
- [x] Frontend: `/admin/ipam/templates` list + editor modal (4 tabs: General, Stamp values, DDNS, Child layout)
- [x] Frontend: optional template combo on `CreateBlockModal` / `CreateSubnetModal` — only renders when ≥1 matching template exists
- [x] Smoke-test (live): 11 assertions — child_layout 422 on subnet template, apply-with-carve, idempotent re-apply, wrong-target 422, create-with-template-id pre-fill, force=true overwrites, reapply-all all pass
- [x] `make ci` clean
- [x] Single commit `feat(ipam): #26 IPAM template classes — reusable stamp templates with sub-subnet child layouts`

### Done when

All checkpoints checked + commit chain landed + merged to main.

## Risks / unknowns

- **Inheritance vs. stamping.** Templates STAMP values at apply
  time; inheritance LOOKS UP values at read time. Don't
  conflate. Re-apply is the operator-driven way to refresh
  stamp values to match the latest template — explicit by
  design, not implicit like inheritance.
- **DDNS column drift.** Subnet has 4–5 DDNS-specific columns
  (`ddns_enabled`, `ddns_hostname_policy`, `ddns_domain_override`,
  `ddns_ttl`, `ddns_inherit_settings`). Mirror them on the
  template — but be careful not to set `ddns_inherit_settings`
  by default since the template's whole point is to lock in a
  config, not to inherit upstream.
- **Child layout edge cases.** Nested child layouts (templates
  applied to children of templates) — out of scope for v1.
  Single-level only.
- **Reapply blast radius.** A template applied to 500 blocks
  with `child_layout` set would carve 500× sub-subnets on
  reapply if the apply path doesn't dedupe. Reapply path must
  be idempotent — skip child carving when the block already
  has children matching the layout.
