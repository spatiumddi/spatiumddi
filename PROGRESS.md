# Branch progress: `feat/block-move`

Single-issue branch for #27 — operator-driven relocation of an
IPBlock (and everything under it) into a different IPSpace.

## Resume protocol

1. `git checkout feat/block-move`
2. Read this file from top to bottom.
3. The first unchecked box under "Checkpoints" is the next thing
   to do.
4. Single feature → single coherent commit at the end (or split
   into model+service / API / UI commits if the diff gets large
   — never squash mid-implementation).
5. When all "Done when" criteria check, run `make ci`, commit,
   merge to `main`.

## Issue #27 — Move IP block / space across IP spaces

### Spec (from issue, with decision points pre-resolved)

> Operator-driven relocation of a block (and everything under it:
> child blocks, subnets, addresses) into a different `IPSpace`.
> Preview + commit endpoints under `/api/v1/ipam/blocks/{id}/move/
> {preview,commit}` mirroring the existing resize UX.

**Decision points already resolved in the issue body:**

1. **Scope.** Block-move only (recursive through descendants).
   Moving a whole space = moving its top-level blocks one-by-one
   through this same endpoint.
2. **Integration-owned rows.** Refuse when any descendant has
   `kubernetes_cluster_id` / `docker_host_id` / future-integration
   FKs set. Preview flags these; commit 409s.
3. **Atomicity.** Single transaction with `SELECT … FOR UPDATE`
   on the block subtree; overlap re-check against the target
   space's existing blocks before the writes land.
4. **Target parent.** Optional. With parent: validate moved
   block is a strict subset. Without: lands at top level of the
   target space; standard overlap-reparent logic applies (same
   rule `create_block` uses to pull existing siblings under a
   supernet).
5. **UI.** `MoveBlockModal` on the block detail header; typed
   CIDR confirmation like resize; preview returns counts.

### Implementation plan

- **Service.** New `app/services/ipam/block_move.py` with
  `preview_move(block_id, target_space_id, target_parent_id?)`
  + `commit_move(...)`. Both share a single
  `_assemble_move_plan` helper:
  - Walk descendants depth-first.
  - Collect block IDs / subnet IDs / address counts /
    integration-owned-flag list.
  - Validate target space exists + operator has write access.
  - If target parent given: validate it's in target space + the
    moved block is a strict subset.
  - If no target parent: re-run the same overlap-vs-existing
    logic `create_block` uses; return any sibling blocks that
    will be pulled under the moved block as a supernet.
  - Re-check overlap against the target space's existing
    block tree.
  - Return a `MovePreview` shape: `{block, target_space,
    target_parent_id?, descendants: {blocks: int, subnets:
    int, addresses: int}, integration_blockers: [{path, type,
    cluster_id?}], reparent_chain: [block_id]?}`.
- **Commit path.** Open transaction with `SELECT … FOR UPDATE`
  on the moved block + recursive descendants + the target
  space's top-level blocks (to lock against concurrent
  `create_block` insertions). Re-run `_assemble_move_plan`;
  abort 409 if anything changed (overlap appeared, integration
  row appeared). Then:
  - UPDATE the moved block: `space_id = target`, `parent_block_id
    = target_parent_id` (or null).
  - Walk descendants and UPDATE `space_id` only — parent FKs
    stay intact.
  - Walk subnets under the moved subtree: UPDATE `space_id`
    (subnet has direct space_id too).
  - Walk addresses: no `space_id` (lives on the subnet); skip.
  - If reparent-chain returned: rewrite the matching top-level
    target-space blocks' `parent_block_id` to point at the
    moved block.
  - Audit-log a single `block.moved` event with the full plan
    in the payload.
- **API.** Two endpoints under `/api/v1/ipam/blocks/{id}`:
  - `POST /move/preview` — body
    `{target_space_id: uuid, target_parent_id?: uuid}` → 200
    `MovePreview`.
  - `POST /move/commit` — same body + `confirmation_cidr: str`
    (must match the moved block's network, mirroring the
    resize UX) → 200 `MoveResult`.
- **Frontend.** `MoveBlockModal` lives next to
  `ResizeBlockModal`. Two-stage form:
  1. Pick target space (combo box, search).
  2. Optional target parent block (filtered to target space).
  3. Click "Preview" → renders the plan + integration-blocker
     list. If blockers: show them in red, disable Submit.
  4. Type the moved block's CIDR to confirm.
  5. "Move" button → POSTs commit. Success toast +
     navigation to the moved block in its new space.
- **Header button.** `MoveBlockModal` opens from a "Move"
  button next to the existing "Resize" button on the block
  detail header. Use `<HeaderButton variant="secondary">` with
  the `Move` lucide icon.
- **Permissions.** Existing `write` on `ip_block` covers it.
  Block-detail page already gates on this.

### Checkpoints

- [ ] Service: `app/services/ipam/block_move.py` with `_assemble_move_plan`, `preview_move`, `commit_move`
- [ ] Router: `POST /api/v1/ipam/blocks/{id}/move/preview` + `/commit`
- [ ] Schemas: `MoveBlockPreview` + `MoveBlockResult` Pydantic models
- [ ] Tests: preview detects integration blockers; commit fails on overlap; reparent-chain works; integration-blocker → 409
- [ ] Frontend: `MoveBlockModal` (use shared `Modal` + draggable hook)
- [ ] Frontend: "Move" header button on `IPBlockDetailPage`
- [ ] `make ci` clean
- [ ] Commit `feat(ipam): #27 block move across IP spaces`

### Done when

All checkpoints checked + commit landed + merged to main.

## Risks / unknowns

- **Subnet `space_id` denormalisation.** Verify whether
  `Subnet.space_id` is a direct column or derived from
  `block.space_id` — if direct, the descendants walk needs to
  update it; if derived, no-op. (Suspect direct based on prior
  code reads — confirm before writing the migration tests.)
- **Audit log payload size.** Big subtree moves could blow up
  the payload. Cap descendant lists to first 1000 IDs in the
  payload; the count field is the source of truth for big
  moves.
- **Cross-VRF moves.** Moving across spaces also crosses VRFs
  if both spaces have a VRF pinned. Surface this in the
  preview as a soft warning; don't block. The operator
  presumably knows.
