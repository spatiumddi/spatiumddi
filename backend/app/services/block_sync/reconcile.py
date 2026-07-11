"""Active block-sync reconciler (#601).

Target-driven convergence: for every *armed* target (an OPNsense router
or a UniFi controller with ``block_sync_enabled=True``), push the
applicable ``network_block`` desired-state onto the device and lift any
block that has been disabled / expired / deleted.

Convergence is non-destructive on the device: SpatiumDDI only removes
IPs / MACs it added (tracked via ``network_block_push`` rows). It never
touches alias members / blocked clients it doesn't own. Active blocks
that are somehow missing from the device (operator removed them by hand)
are re-pushed — the block set is the source of truth (NN#4/#9).

* OPNsense — kind=``ip`` blocks → firewall table-alias membership. We
  read the current alias members so we can add only what's missing and
  reconfigure once.
* UniFi — kind=``mac`` blocks → ``block-sta`` / ``unblock-sta`` L2
  quarantine. No cheap "list blocked" read, so convergence is driven
  purely off push rows + the idempotent block/unblock commands.

Every push path is gated: the caller must ensure the ``security.block_sync``
feature module is on; the per-target ``block_sync_enabled`` master switch
is checked here; and a target with no write credentials is skipped with a
clear error (never a silent fall-back to read creds).
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str
from app.models.audit import AuditLog
from app.models.block_sync import NetworkBlock, NetworkBlockPush
from app.models.opnsense import OPNsenseRouter
from app.models.panos import PANOSFirewall
from app.models.unifi import UnifiController
from app.services.opnsense.client import OPNsenseClient, OPNsenseClientError
from app.services.oui import normalize_mac_key
from app.services.panos.client import PANOSClient, PANOSClientError
from app.services.unifi.client import UnifiClient, UnifiClientConfig, UnifiClientError

# How often a UniFi block is re-asserted even when its push row is already
# "pushed". UniFi has no cheap "list blocked" read, so a MAC an operator
# manually unblocks on the controller would otherwise never be re-quarantined
# (the block set is the source of truth — #601 review). Re-issuing block-sta is
# idempotent; the window trades convergence latency for controller load.
_UNIFI_REASSERT_SECONDS = 3600.0


def _audit_device_push(
    db: AsyncSession,
    target_kind: str,
    target_id: uuid.UUID,
    target_name: str,
    added: list[str],
    removed: list[str],
) -> None:
    """Audit the actual device mutation (NN#4). The reconciler runs from the
    Celery sweep / on-create task with no request user, so the row is attributed
    to the system with the applied add/remove diff captured in ``new_value``."""
    if not added and not removed:
        return
    db.add(
        AuditLog(
            user_id=None,
            user_display_name="system",
            auth_source="system",
            action="block_sync_push",
            resource_type="network_block_target",
            resource_id=str(target_id),
            resource_display=f"{target_kind}:{target_name}",
            new_value={"added": added, "removed": removed},
        )
    )


# ── Value + target helpers (shared by the router + the gated operation) ──


def normalize_block_value(kind: str, value: str) -> str:
    """Canonicalise a block value. Raises ``ValueError`` on bad input.

    ``ip`` → the string form of a parsed IP address; ``mac`` → the
    lowercase colon-separated 12-hex form UniFi + Kea both expect.
    """
    value = value.strip()
    if kind == "ip":
        return str(ipaddress.ip_address(value))
    if kind == "mac":
        key = normalize_mac_key(value)
        if key is None:
            raise ValueError(f"invalid MAC address: {value}")
        return ":".join(key[i : i + 2] for i in range(0, 12, 2))
    raise ValueError(f"invalid block kind: {kind}")


async def applicable_targets_for_kind(db: AsyncSession, kind: str) -> list[tuple[str, uuid.UUID]]:
    """Armed targets that consume a block of ``kind`` — OPNsense (firewall
    alias) for IPs, UniFi (L2 quarantine) for MACs."""
    out: list[tuple[str, uuid.UUID]] = []
    if kind == "ip":
        rows = (
            (
                await db.execute(
                    select(OPNsenseRouter.id).where(OPNsenseRouter.block_sync_enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        out.extend(("opnsense", rid) for rid in rows)
        panos_rows = (
            (
                await db.execute(
                    select(PANOSFirewall.id).where(PANOSFirewall.block_sync_enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        out.extend(("paloalto", pid) for pid in panos_rows)
    elif kind == "mac":
        rows = (
            (
                await db.execute(
                    select(UnifiController.id).where(UnifiController.block_sync_enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        out.extend(("unifi", cid) for cid in rows)
    return out


# ── Result / diff shapes ─────────────────────────────────────────────


@dataclass
class TargetDiff:
    """What a reconcile pass *would* change on one target (preview)."""

    target_kind: str  # "opnsense" | "unifi"
    target_id: uuid.UUID
    target_name: str
    to_add: list[str] = field(default_factory=list)  # values to push
    to_remove: list[str] = field(default_factory=list)  # values to lift
    # Populated when the target can't be previewed/pushed (creds missing,
    # not armed, device unreachable). A non-empty error means no changes.
    error: str | None = None

    def is_noop(self) -> bool:
        return not self.to_add and not self.to_remove


@dataclass
class BlockSyncSummary:
    target_kind: str
    target_id: uuid.UUID
    target_name: str
    ok: bool = True
    error: str | None = None
    added: int = 0
    removed: int = 0
    errors: int = 0


# ── Helpers ──────────────────────────────────────────────────────────


def block_is_active(block: NetworkBlock, now: datetime) -> bool:
    """A block should be enforced iff it is enabled and not past its
    optional ``expires_at``."""
    if not block.enabled:
        return False
    if block.expires_at is not None and block.expires_at <= now:
        return False
    return True


async def _load_blocks(db: AsyncSession, kind: str) -> list[NetworkBlock]:
    return list(
        (await db.execute(select(NetworkBlock).where(NetworkBlock.kind == kind))).scalars().all()
    )


async def _load_pushes(
    db: AsyncSession, target_kind: str, target_id: uuid.UUID
) -> dict[uuid.UUID, NetworkBlockPush]:
    """Existing push rows for this target, keyed by ``block_id``."""
    rows = (
        (
            await db.execute(
                select(NetworkBlockPush).where(
                    NetworkBlockPush.target_kind == target_kind,
                    NetworkBlockPush.target_id == target_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.block_id: r for r in rows}


# ── OPNsense ─────────────────────────────────────────────────────────


def _opnsense_client(router: OPNsenseRouter) -> OPNsenseClient:
    secret = decrypt_str(router.block_sync_api_secret_encrypted)
    return OPNsenseClient(
        host=router.host,
        port=router.port,
        api_key=router.block_sync_api_key,
        api_secret=secret,
        verify_tls=router.verify_tls,
        ca_bundle_pem=router.ca_bundle_pem,
    )


def opnsense_config_error(router: OPNsenseRouter) -> str | None:
    if not router.block_sync_enabled:
        return "block sync not armed on this target"
    if not router.block_alias_name.strip():
        return "no firewall alias configured (block_alias_name)"
    if not router.block_sync_api_key.strip() or not router.block_sync_api_secret_encrypted:
        return "write-scoped credentials not configured"
    return None


async def preview_opnsense(
    db: AsyncSession, router: OPNsenseRouter, *, read_device: bool = True
) -> TargetDiff:
    """Compute the add/remove diff for one OPNsense target without pushing."""
    diff = TargetDiff(target_kind="opnsense", target_id=router.id, target_name=router.name)
    cfg_err = opnsense_config_error(router)
    if cfg_err:
        diff.error = cfg_err
        return diff

    now = datetime.now(UTC)
    blocks = await _load_blocks(db, "ip")
    active = {b.value for b in blocks if block_is_active(b, now)}
    pushes = await _load_pushes(db, "opnsense", router.id)
    block_by_id = {b.id: b for b in blocks}

    # Values we currently own on this target (pushed / pending / error).
    owned = {block_by_id[bid].value for bid in pushes if bid in block_by_id}
    # Lift: owned values whose block is no longer active (or deleted).
    for bid, push in pushes.items():
        b = block_by_id.get(bid)
        if b is None or not block_is_active(b, now):
            diff.to_remove.append(push_value_hint(push, b))

    on_device: set[str] = set()
    if read_device:
        try:
            async with _opnsense_client(router) as client:
                on_device = set(await client.alias_list_addresses(router.block_alias_name.strip()))
        except OPNsenseClientError as exc:
            diff.error = f"cannot read alias: {exc}"
            return diff

    # Add: active value not yet owned, OR owned-but-missing-from-device.
    for value in sorted(active):
        if value not in owned or (read_device and value not in on_device):
            diff.to_add.append(value)

    return diff


def push_value_hint(push: NetworkBlockPush, block: NetworkBlock | None) -> str:
    """Best-effort value for a to-remove entry (the block row may be gone)."""
    if block is not None:
        return block.value
    return f"<block {push.block_id}>"


async def reconcile_opnsense(db: AsyncSession, router: OPNsenseRouter) -> BlockSyncSummary:
    """Push the OPNsense target to match desired state. Commits push-row
    changes; the caller commits the target's timestamp fields."""
    summary = BlockSyncSummary(target_kind="opnsense", target_id=router.id, target_name=router.name)
    cfg_err = opnsense_config_error(router)
    if cfg_err:
        summary.ok = False
        summary.error = cfg_err
        router.last_block_sync_error = cfg_err
        return summary

    now = datetime.now(UTC)
    alias = router.block_alias_name.strip()
    blocks = await _load_blocks(db, "ip")
    block_by_id = {b.id: b for b in blocks}
    active_blocks = [b for b in blocks if block_is_active(b, now)]
    pushes = await _load_pushes(db, "opnsense", router.id)

    changed = False
    added_vals: list[str] = []
    removed_vals: list[str] = []
    # Blocks whose membership is (re)asserted this pass but whose push row must
    # not be marked "pushed" until reconfigure actually loads the ruleset — so a
    # failed reconfigure never reports a block as converged (#601 review #6).
    to_confirm: list[tuple[NetworkBlock, NetworkBlockPush | None]] = []
    try:
        async with _opnsense_client(router) as client:
            on_device = set(await client.alias_list_addresses(alias))

            # Lift blocks that are no longer active (or whose row is gone).
            for bid, push in list(pushes.items()):
                b = block_by_id.get(bid)
                if b is not None and block_is_active(b, now):
                    continue
                value = b.value if b is not None else None
                try:
                    if value and value in on_device:
                        await client.alias_delete_address(alias, value)
                        changed = True
                        removed_vals.append(value)
                    await db.delete(push)
                    summary.removed += 1
                except OPNsenseClientError as exc:
                    push.push_status = "error"
                    push.last_error = str(exc)
                    summary.errors += 1

            # Add / re-assert active blocks.
            for b in active_blocks:
                push = pushes.get(b.id)
                confirmed = push is not None and push.push_status == "pushed"
                on_dev = b.value in on_device
                if confirmed and on_dev:
                    continue  # already converged
                try:
                    if not on_dev:
                        await client.alias_add_address(alias, b.value)
                        changed = True
                        added_vals.append(b.value)
                        summary.added += 1
                    # Defer marking "pushed" until reconfigure succeeds below.
                    to_confirm.append((b, push))
                except OPNsenseClientError as exc:
                    _upsert_push(
                        db, push, b.id, "opnsense", router.id, now, status="error", error=str(exc)
                    )
                    summary.errors += 1

            # Reconfigure applies both removals and adds. Run it whenever
            # something changed OR there are memberships awaiting confirmation
            # (the latter covers retrying a prior pass whose reconfigure failed
            # while the IP already sits in the alias table).
            if changed or to_confirm:
                try:
                    await client.alias_reconfigure()
                except OPNsenseClientError as exc:
                    # The ruleset was NOT reloaded — do not claim these as
                    # pushed; mark them errored so the next sweep retries.
                    for b, push in to_confirm:
                        _upsert_push(
                            db,
                            push,
                            b.id,
                            "opnsense",
                            router.id,
                            now,
                            status="error",
                            error=f"reconfigure failed: {exc}",
                        )
                    summary.errors += len(to_confirm)
                    summary.ok = False
                    summary.error = str(exc)
                    router.last_block_sync_error = f"reconfigure failed: {exc}"
                    router.last_block_sync_at = now
                    return summary

            # Reconfigure succeeded (or nothing to apply) — confirm pushes.
            for b, push in to_confirm:
                _upsert_push(db, push, b.id, "opnsense", router.id, now)
    except OPNsenseClientError as exc:
        summary.ok = False
        summary.error = str(exc)
        router.last_block_sync_error = str(exc)
        return summary

    router.last_block_sync_at = now
    router.last_block_sync_error = (
        None if summary.errors == 0 else f"{summary.errors} push error(s)"
    )
    summary.ok = summary.errors == 0
    _audit_device_push(db, "opnsense", router.id, router.name, added_vals, removed_vals)
    return summary


# ── UniFi ────────────────────────────────────────────────────────────


def _unifi_client(controller: UnifiController) -> UnifiClient:
    kind = controller.block_sync_auth_kind or "api_key"
    api_key = (
        decrypt_str(controller.block_sync_api_key_encrypted)
        if controller.block_sync_api_key_encrypted
        else ""
    )
    username = (
        decrypt_str(controller.block_sync_username_encrypted)
        if controller.block_sync_username_encrypted
        else ""
    )
    password = (
        decrypt_str(controller.block_sync_password_encrypted)
        if controller.block_sync_password_encrypted
        else ""
    )
    return UnifiClient(
        UnifiClientConfig(
            mode=controller.mode,
            host=controller.host,
            port=controller.port,
            cloud_host_id=controller.cloud_host_id,
            verify_tls=controller.verify_tls,
            ca_bundle_pem=controller.ca_bundle_pem or "",
            auth_kind=kind,
            api_key=api_key,
            username=username,
            password=password,
        )
    )


def unifi_config_error(controller: UnifiController) -> str | None:
    if not controller.block_sync_enabled:
        return "block sync not armed on this target"
    kind = controller.block_sync_auth_kind or "api_key"
    if kind == "api_key" and not controller.block_sync_api_key_encrypted:
        return "write-scoped API key not configured"
    if kind == "user_password" and not (
        controller.block_sync_username_encrypted and controller.block_sync_password_encrypted
    ):
        return "write-scoped admin username/password not configured"
    if kind == "user_password" and controller.mode == "cloud":
        return "cloud controllers require an API key for block sync"
    return None


def _unifi_site(controller: UnifiController) -> str:
    return (controller.block_sync_site or "default").strip() or "default"


async def preview_unifi(db: AsyncSession, controller: UnifiController) -> TargetDiff:
    """Compute the add/remove diff for one UniFi target (push-row driven —
    the controller has no cheap 'list blocked' read)."""
    diff = TargetDiff(target_kind="unifi", target_id=controller.id, target_name=controller.name)
    cfg_err = unifi_config_error(controller)
    if cfg_err:
        diff.error = cfg_err
        return diff

    now = datetime.now(UTC)
    blocks = await _load_blocks(db, "mac")
    block_by_id = {b.id: b for b in blocks}
    pushes = await _load_pushes(db, "unifi", controller.id)
    owned = {block_by_id[bid].value for bid in pushes if bid in block_by_id}

    for b in blocks:
        if block_is_active(b, now) and (
            b.value not in owned or pushes[b.id].push_status != "pushed"
        ):
            diff.to_add.append(b.value)
    for bid, push in pushes.items():
        b = block_by_id.get(bid)
        if b is None or not block_is_active(b, now):
            diff.to_remove.append(push_value_hint(push, b))
    return diff


async def reconcile_unifi(db: AsyncSession, controller: UnifiController) -> BlockSyncSummary:
    summary = BlockSyncSummary(
        target_kind="unifi", target_id=controller.id, target_name=controller.name
    )
    cfg_err = unifi_config_error(controller)
    if cfg_err:
        summary.ok = False
        summary.error = cfg_err
        controller.last_block_sync_error = cfg_err
        return summary

    now = datetime.now(UTC)
    site = _unifi_site(controller)
    blocks = await _load_blocks(db, "mac")
    block_by_id = {b.id: b for b in blocks}
    active_blocks = [b for b in blocks if block_is_active(b, now)]
    pushes = await _load_pushes(db, "unifi", controller.id)
    added_vals: list[str] = []
    removed_vals: list[str] = []

    try:
        async with _unifi_client(controller) as client:
            # Lift.
            for bid, push in list(pushes.items()):
                b = block_by_id.get(bid)
                if b is not None and block_is_active(b, now):
                    continue
                try:
                    if b is not None:
                        await client.unblock_client(site, b.value)
                        removed_vals.append(b.value)
                    await db.delete(push)
                    summary.removed += 1
                except UnifiClientError as exc:
                    push.push_status = "error"
                    push.last_error = str(exc)
                    summary.errors += 1

            # (Re)assert active blocks. block-sta is idempotent; because UniFi
            # has no "list blocked" read, a MAC an operator manually unblocked
            # on the controller must be periodically re-asserted or it silently
            # stays un-quarantined while we report converged (#601 review #9).
            for b in active_blocks:
                push = pushes.get(b.id)
                is_pushed = push is not None and push.push_status == "pushed"
                stale = (
                    push is None
                    or push.last_pushed_at is None
                    or (now - push.last_pushed_at).total_seconds() >= _UNIFI_REASSERT_SECONDS
                )
                if is_pushed and not stale:
                    continue
                try:
                    await client.block_client(site, b.value)
                    _upsert_push(db, push, b.id, "unifi", controller.id, now)
                    if not is_pushed:
                        added_vals.append(b.value)
                        summary.added += 1
                except UnifiClientError as exc:
                    _upsert_push(
                        db, push, b.id, "unifi", controller.id, now, status="error", error=str(exc)
                    )
                    summary.errors += 1
    except UnifiClientError as exc:
        summary.ok = False
        summary.error = str(exc)
        controller.last_block_sync_error = str(exc)
        return summary

    controller.last_block_sync_at = now
    controller.last_block_sync_error = (
        None if summary.errors == 0 else f"{summary.errors} push error(s)"
    )
    summary.ok = summary.errors == 0
    _audit_device_push(db, "unifi", controller.id, controller.name, added_vals, removed_vals)
    return summary


# ── Palo Alto PAN-OS (Dynamic Address Group tag register, #605) ──────
#
# kind=``ip`` blocks → an ``IP → tag`` User-ID registration (no policy commit).
# The operator pre-creates a Dynamic Address Group whose match is the target's
# ``block_tag_name``; registering the tag enforces the block near-instantly.
# Convergence reads current registered IPs for the tag (``show object
# registered-ip tag <t>``) so we add only what's missing and remove only what
# we own — never diff against a bad-empty set (NN#5).


def _panos_client(fw: PANOSFirewall) -> PANOSClient:
    key = decrypt_str(fw.block_sync_api_key_encrypted)
    return PANOSClient(
        host=fw.host,
        port=fw.port,
        api_key=key,
        api_version=fw.api_version,
        is_panorama=fw.is_panorama,
        vsys=fw.vsys,
        device_group=fw.device_group,
        verify_tls=fw.verify_tls,
        ca_bundle_pem=fw.ca_bundle_pem,
    )


def panos_config_error(fw: PANOSFirewall) -> str | None:
    if not fw.block_sync_enabled:
        return "block sync not armed on this target"
    if fw.is_panorama:
        # User-ID tag registration targets a firewall vsys, not Panorama.
        return "DAG enforcement requires a standalone firewall (vsys), not a Panorama target"
    if not fw.block_tag_name.strip():
        return "no DAG tag configured (block_tag_name)"
    if not fw.block_sync_api_key_encrypted:
        return "User-ID write-scoped API key not configured"
    return None


async def preview_panos(
    db: AsyncSession, fw: PANOSFirewall, *, read_device: bool = True
) -> TargetDiff:
    diff = TargetDiff(target_kind="paloalto", target_id=fw.id, target_name=fw.name)
    cfg_err = panos_config_error(fw)
    if cfg_err:
        diff.error = cfg_err
        return diff

    now = datetime.now(UTC)
    blocks = await _load_blocks(db, "ip")
    active = {b.value for b in blocks if block_is_active(b, now)}
    pushes = await _load_pushes(db, "paloalto", fw.id)
    block_by_id = {b.id: b for b in blocks}

    owned = {block_by_id[bid].value for bid in pushes if bid in block_by_id}
    for bid, push in pushes.items():
        b = block_by_id.get(bid)
        if b is None or not block_is_active(b, now):
            diff.to_remove.append(push_value_hint(push, b))

    on_device: set[str] = set()
    if read_device:
        try:
            async with _panos_client(fw) as client:
                regs = await client.list_registered_ips(fw.block_tag_name.strip())
                on_device = {r.ip for r in regs}
        except PANOSClientError as exc:
            diff.error = f"cannot read registered IPs: {exc}"
            return diff

    for value in sorted(active):
        if value not in owned or (read_device and value not in on_device):
            diff.to_add.append(value)
    return diff


async def reconcile_panos(db: AsyncSession, fw: PANOSFirewall) -> BlockSyncSummary:
    """Push the PAN-OS target to match desired state: register active IP blocks
    as ``IP → tag`` and unregister lifted/expired/deleted ones. Commits push-row
    changes; the caller commits the target's timestamp fields."""
    summary = BlockSyncSummary(target_kind="paloalto", target_id=fw.id, target_name=fw.name)
    cfg_err = panos_config_error(fw)
    if cfg_err:
        summary.ok = False
        summary.error = cfg_err
        fw.last_block_sync_error = cfg_err
        return summary

    now = datetime.now(UTC)
    tag = fw.block_tag_name.strip()
    blocks = await _load_blocks(db, "ip")
    block_by_id = {b.id: b for b in blocks}
    active_blocks = [b for b in blocks if block_is_active(b, now)]
    pushes = await _load_pushes(db, "paloalto", fw.id)
    added_vals: list[str] = []
    removed_vals: list[str] = []

    try:
        async with _panos_client(fw) as client:
            regs = await client.list_registered_ips(tag)
            on_device = {r.ip for r in regs}

            # Unregister blocks that are no longer active (or whose row is gone).
            for bid, push in list(pushes.items()):
                b = block_by_id.get(bid)
                if b is not None and block_is_active(b, now):
                    continue
                value = b.value if b is not None else None
                try:
                    if value and value in on_device:
                        await client.unregister_ip_tag(value, tag)
                        removed_vals.append(value)
                    await db.delete(push)
                    summary.removed += 1
                except PANOSClientError as exc:
                    push.push_status = "error"
                    push.last_error = str(exc)
                    summary.errors += 1

            # Register / re-assert active blocks.
            for b in active_blocks:
                push = pushes.get(b.id)
                confirmed = push is not None and push.push_status == "pushed"
                on_dev = b.value in on_device
                if confirmed and on_dev:
                    continue
                try:
                    if not on_dev:
                        await client.register_ip_tag(b.value, tag)
                        added_vals.append(b.value)
                        summary.added += 1
                    _upsert_push(db, push, b.id, "paloalto", fw.id, now)
                except PANOSClientError as exc:
                    _upsert_push(
                        db, push, b.id, "paloalto", fw.id, now, status="error", error=str(exc)
                    )
                    summary.errors += 1
    except PANOSClientError as exc:
        summary.ok = False
        summary.error = str(exc)
        fw.last_block_sync_error = str(exc)
        return summary

    fw.last_block_sync_at = now
    fw.last_block_sync_error = None if summary.errors == 0 else f"{summary.errors} push error(s)"
    summary.ok = summary.errors == 0
    _audit_device_push(db, "paloalto", fw.id, fw.name, added_vals, removed_vals)
    return summary


# ── Shared push-row upsert ───────────────────────────────────────────


def _upsert_push(
    db: AsyncSession,
    push: NetworkBlockPush | None,
    block_id: uuid.UUID,
    target_kind: str,
    target_id: uuid.UUID,
    now: datetime,
    *,
    status: str = "pushed",
    error: str | None = None,
) -> None:
    if push is None:
        push = NetworkBlockPush(
            block_id=block_id,
            target_kind=target_kind,
            target_id=target_id,
        )
        db.add(push)
    push.push_status = status
    push.last_error = error
    if status == "pushed":
        push.last_pushed_at = now


# ── Fan-out ──────────────────────────────────────────────────────────


async def armed_targets(
    db: AsyncSession,
) -> tuple[list[OPNsenseRouter], list[UnifiController], list[PANOSFirewall]]:
    routers = list(
        (
            await db.execute(
                select(OPNsenseRouter).where(OPNsenseRouter.block_sync_enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    controllers = list(
        (
            await db.execute(
                select(UnifiController).where(UnifiController.block_sync_enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    firewalls = list(
        (await db.execute(select(PANOSFirewall).where(PANOSFirewall.block_sync_enabled.is_(True))))
        .scalars()
        .all()
    )
    return routers, controllers, firewalls


async def lift_all_for_target(
    db: AsyncSession, target_kind: str, target: OPNsenseRouter | UnifiController | PANOSFirewall
) -> BlockSyncSummary:
    """Remove EVERY value SpatiumDDI pushed to a target and delete its push
    rows — used when a target is disarmed (block_sync_enabled → False), so a
    disabled firewall/gateway doesn't keep enforcing blocks with no reconcile
    path (#601 review #8). Deliberately does NOT gate on block_sync_enabled
    (the target is being disarmed); it only needs the write creds still present.
    """
    summary = BlockSyncSummary(
        target_kind=target_kind, target_id=target.id, target_name=target.name
    )
    pushes = await _load_pushes(db, target_kind, target.id)
    if not pushes:
        return summary
    block_by_id = {b.id: b for b in (await db.execute(select(NetworkBlock))).scalars().all()}
    removed_vals: list[str] = []
    try:
        if target_kind == "opnsense":
            router = target
            assert isinstance(router, OPNsenseRouter)
            if not router.block_alias_name.strip() or not router.block_sync_api_key:
                return summary  # no creds/alias to work with; leave rows as-is
            alias = router.block_alias_name.strip()
            changed = False
            async with _opnsense_client(router) as client:
                on_device = set(await client.alias_list_addresses(alias))
                for bid, push in list(pushes.items()):
                    b = block_by_id.get(bid)
                    value = b.value if b is not None else None
                    if value and value in on_device:
                        await client.alias_delete_address(alias, value)
                        changed = True
                        removed_vals.append(value)
                    await db.delete(push)
                    summary.removed += 1
                if changed:
                    await client.alias_reconfigure()
        elif target_kind == "unifi":
            controller = target
            assert isinstance(controller, UnifiController)
            kind = controller.block_sync_auth_kind or "api_key"
            creds_ok = (
                bool(controller.block_sync_api_key_encrypted)
                if kind == "api_key"
                else bool(
                    controller.block_sync_username_encrypted
                    and controller.block_sync_password_encrypted
                )
            )
            if not creds_ok:
                return summary  # no creds to reach the controller; leave rows
            site = _unifi_site(controller)
            async with _unifi_client(controller) as client:
                for bid, push in list(pushes.items()):
                    b = block_by_id.get(bid)
                    if b is not None:
                        await client.unblock_client(site, b.value)
                        removed_vals.append(b.value)
                    await db.delete(push)
                    summary.removed += 1
        else:
            fw = target
            assert isinstance(fw, PANOSFirewall)
            if not fw.block_tag_name.strip() or not fw.block_sync_api_key_encrypted:
                return summary  # no tag/creds to work with; leave rows as-is
            tag = fw.block_tag_name.strip()
            async with _panos_client(fw) as client:
                regs = await client.list_registered_ips(tag)
                on_device = {r.ip for r in regs}
                for bid, push in list(pushes.items()):
                    b = block_by_id.get(bid)
                    if b is not None and b.value in on_device:
                        await client.unregister_ip_tag(b.value, tag)
                        removed_vals.append(b.value)
                    await db.delete(push)
                    summary.removed += 1
    except (OPNsenseClientError, UnifiClientError, PANOSClientError) as exc:
        summary.ok = False
        summary.error = str(exc)
        target.last_block_sync_error = f"disarm-lift failed: {exc}"
        return summary

    target.last_block_sync_at = datetime.now(UTC)
    _audit_device_push(db, target_kind, target.id, target.name, [], removed_vals)
    return summary


async def preview_all(db: AsyncSession, *, read_device: bool = True) -> list[TargetDiff]:
    """Preview every armed target (used by the reconcile-all preview)."""
    routers, controllers, firewalls = await armed_targets(db)
    diffs: list[TargetDiff] = []
    for r in routers:
        diffs.append(await preview_opnsense(db, r, read_device=read_device))
    for c in controllers:
        diffs.append(await preview_unifi(db, c))
    for fw in firewalls:
        diffs.append(await preview_panos(db, fw, read_device=read_device))
    return diffs


__all__ = [
    "BlockSyncSummary",
    "TargetDiff",
    "applicable_targets_for_kind",
    "armed_targets",
    "block_is_active",
    "lift_all_for_target",
    "normalize_block_value",
    "opnsense_config_error",
    "panos_config_error",
    "preview_all",
    "preview_opnsense",
    "preview_panos",
    "preview_unifi",
    "reconcile_opnsense",
    "reconcile_panos",
    "reconcile_unifi",
    "unifi_config_error",
]
