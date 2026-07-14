"""Pre-flight safety checks for a multi-node rolling upgrade (#296 Phase A).

Phase A ships read-only checks the operator runs **before** committing
to an upgrade. The endpoint at ``GET /api/v1/upgrades/preflight?target=<tag>``
calls ``run_all()`` and returns the aggregate; nothing here mutates
cluster state.

Each check is its own async function returning a ``PreflightResult``.
We deliberately keep them independent — a failure in any one shouldn't
stop the others from running, so the operator sees the full picture
in one round-trip. The endpoint serializes the result list + an
``overall`` summary; the UI renders red/amber/green per row.

Checks shipped in Phase A:

* ``check_inflight_conflict`` — another upgrade in flight (lease state).
* ``check_replication_lag`` — CNPG replicas caught up (queryable via SQL).
* ``check_disk_headroom`` — ``/var`` partition has room for the slot
  image + a safety margin.
* ``check_version_path`` — target tag is a valid forward jump from
  ``settings.version``; no skip-release across the supported skew window.
* ``check_kea_ha_version_skew`` — a Kea HA pair on pre-3.0 cannot be
  upgraded node-at-a-time (3.0's HA hook won't talk to a < 2.7 peer);
  warn the operator before they start. Scoped to appliance nodes and to
  real HA pairs only, and never returns ``fail`` (blocking would strand a
  broken pair in its broken state). See issue #637.
* ``check_quorum`` — cluster size is odd + ≥ 3 + every node currently
  Ready (so we don't start a rolling upgrade with a node already down).

What we **don't** check here that Phases C/D will own:

* Live etcd member health (the orchestrator owns the per-node etcd
  rejoin gate).
* DaemonSet readiness on each node (gated in the per-node primitive,
  not at preflight time — the probe lands in A2).
* Slot-image presence on the mirror node (Phase B's responsibility
  to surface its own readiness).
"""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

import structlog
from sqlalchemy import text

from app.config import settings
from app.db import AsyncSessionLocal
from app.services.upgrades import mutex

logger = structlog.get_logger(__name__)


PreflightLevel = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class PreflightResult:
    """One check's outcome.

    ``level``:
      * ``ok``    — safe to proceed (rendered green)
      * ``warn``  — degraded but upgrade can still start; surface to
        operator (rendered amber)
      * ``fail``  — must be resolved before starting (rendered red,
        blocks the Start button)

    ``detail`` is structured data the UI can render inline (e.g. the
    replication-lag bytes per replica). ``message`` is the one-line
    human summary.
    """

    name: str
    level: PreflightLevel
    message: str
    detail: dict[str, Any]


# CalVer tag format: YYYY.MM.DD-N.  See CLAUDE.md "Version Scheme".
_CALVER_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})-(\d+)$")


def _parse_calver(tag: str) -> tuple[int, int, int, int] | None:
    m = _CALVER_RE.match(tag)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


# ── Individual checks ─────────────────────────────────────────────────


def check_inflight_conflict(*, namespace: str | None = None) -> PreflightResult:
    """Refuses if another upgrade is already in flight cluster-wide.

    Reads the ``spatium-upgrade-lock`` Lease; if it's held + not
    expired we ``fail`` with the holder's identity. An expired lease
    is fine (the previous holder crashed before releasing — we'll
    take over on acquire).
    """
    state = mutex.get_state(namespace=namespace)
    if state.held and not state.expired:
        return PreflightResult(
            name="inflight_conflict",
            level="fail",
            message=f"another upgrade is in flight (lease held by {state.holder!r})",
            detail={
                "holder": state.holder,
                "renew_time": state.renew_time,
                "transitions": state.transitions,
            },
        )
    return PreflightResult(
        name="inflight_conflict",
        level="ok",
        message="no upgrade in flight",
        detail={
            "previous_holder": state.holder,
            "previous_transitions": state.transitions,
        },
    )


async def check_replication_lag(*, threshold_bytes: int = 16 * 1024) -> PreflightResult:
    """Verify every CNPG replica is caught up enough for a switchover.

    Reads ``pg_stat_replication`` via the api's existing connection.
    Each replica row should have ``state='streaming'`` and
    ``pg_wal_lsn_diff(sent_lsn, replay_lsn) <= threshold_bytes``.

    A lagging replica isn't a fail (the orchestrator will pick a
    different replica for the switchover) but >0 lag is a ``warn``
    so the operator knows the picture before clicking Start.
    """
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text("""
                        SELECT
                            application_name,
                            state,
                            pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
                        FROM pg_stat_replication
                        """))).all()
    except Exception as exc:  # noqa: BLE001 — surface any DB error
        logger.warning("preflight_replication_query_failed", error=str(exc))
        return PreflightResult(
            name="replication_lag",
            level="warn",
            message=f"could not query pg_stat_replication: {exc}",
            detail={"error": str(exc)},
        )
    replicas = [
        {
            "name": r.application_name,
            "state": r.state,
            "lag_bytes": int(r.lag_bytes or 0),
        }
        for r in rows
    ]
    # Single-node CNPG (or no CNPG) — no replicas to check; not a
    # rolling-upgrade fail because the standalone shape never tries one.
    if not replicas:
        return PreflightResult(
            name="replication_lag",
            level="ok",
            message="no streaming replicas (single-node shape)",
            detail={"replicas": []},
        )
    streaming = [r for r in replicas if r["state"] == "streaming"]
    lagging = [r for r in streaming if r["lag_bytes"] > threshold_bytes]
    not_streaming = [r for r in replicas if r["state"] != "streaming"]
    if not_streaming:
        return PreflightResult(
            name="replication_lag",
            level="fail",
            message=(
                f"{len(not_streaming)} replica(s) not streaming "
                "— refuse to start; resolve before upgrading"
            ),
            detail={"replicas": replicas},
        )
    if lagging:
        return PreflightResult(
            name="replication_lag",
            level="warn",
            message=(
                f"{len(lagging)} replica(s) lagging > {threshold_bytes} B "
                "— orchestrator will pick a caught-up replica per node"
            ),
            detail={"replicas": replicas, "threshold_bytes": threshold_bytes},
        )
    return PreflightResult(
        name="replication_lag",
        level="ok",
        message=f"{len(streaming)} replica(s) streaming + caught up",
        detail={"replicas": replicas},
    )


def check_disk_headroom(
    *,
    var_path: str = "/var",
    slot_image_size_bytes: int = 4 * 1024 * 1024 * 1024,
    safety_margin_bytes: int = 1 * 1024 * 1024 * 1024,
) -> PreflightResult:
    """``/var`` has room for the slot image + a safety margin.

    The slot image lives on ``/var/lib/spatiumddi/slot-images/`` (or is
    streamed from the mirror node) and gets ``dd``-ed onto the inactive
    root partition. We need free space for:

    * The downloaded image (~1-4 GiB).
    * Headroom for etcd snapshots that the upgrade triggers
      (``etcd-snapshot save`` runs as a pre-upgrade safety net).
    * ``/var`` operator margin (logs, container layers).

    Default budget: 4 GiB slot image + 1 GiB margin = 5 GiB. Tune via
    args if the operator picks a non-default slot image size.
    """
    try:
        usage = shutil.disk_usage(var_path)
    except OSError as exc:
        return PreflightResult(
            name="disk_headroom",
            level="warn",
            message=f"could not stat {var_path}: {exc}",
            detail={"path": var_path, "error": str(exc)},
        )
    need = slot_image_size_bytes + safety_margin_bytes
    if usage.free < need:
        return PreflightResult(
            name="disk_headroom",
            level="fail",
            message=(
                f"{var_path} has {usage.free // (1024**3)} GiB free; "
                f"need {need // (1024**3)} GiB (slot image + margin)"
            ),
            detail={
                "path": var_path,
                "free_bytes": usage.free,
                "needed_bytes": need,
                "total_bytes": usage.total,
            },
        )
    return PreflightResult(
        name="disk_headroom",
        level="ok",
        message=(
            f"{var_path}: {usage.free // (1024**3)} GiB free " f"(need {need // (1024**3)} GiB)"
        ),
        detail={
            "path": var_path,
            "free_bytes": usage.free,
            "needed_bytes": need,
            "total_bytes": usage.total,
        },
    )


async def check_mirror_disk_headroom(
    *,
    slot_image_size_bytes: int = 4 * 1024 * 1024 * 1024,
    safety_margin_bytes: int = 1 * 1024 * 1024 * 1024,
) -> PreflightResult:
    """Mirror node has room for the slot image + a safety margin.

    Phase B-only check — only fires when ``settings.slot_image_mirror_url``
    is set. Queries the mirror's ``/api/v1/appliance/internal/slot-
    images/_/disk-usage`` endpoint over the in-cluster Service to get
    the real PVC volume's free space; ``check_disk_headroom`` above
    looks at the api pod's /var which isn't where the slot image
    actually lands in mirror mode.

    On docker-compose / non-mirror shapes returns ``ok`` with detail
    noting "no mirror configured" so the operator-facing report still
    shows the row but doesn't surface a false warning.
    """
    if not settings.slot_image_mirror_url:
        return PreflightResult(
            name="mirror_disk_headroom",
            level="ok",
            message="no mirror configured — local disk_headroom covers it",
            detail={"mirror_url": ""},
        )

    # Inline imports — the mirror client uses httpx which is a heavy-ish
    # import. Skipping it on the docker-compose path keeps the cold-
    # start cost on those deploys identical to pre-Phase-B.
    import httpx  # noqa: PLC0415

    from app.api.v1.appliance.slot_image_mirror import mirror_auth_token  # noqa: PLC0415

    zero_id = uuid.UUID(int=0)
    url = f"{settings.slot_image_mirror_url.rstrip('/')}/api/v1/appliance/internal/slot-images/_/disk-usage"
    headers = {"X-Mirror-Auth": mirror_auth_token("disk-usage", zero_id)}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return PreflightResult(
            name="mirror_disk_headroom",
            level="warn",
            message=f"could not reach mirror Service: {exc}",
            detail={"url": url, "error": str(exc)},
        )
    if resp.status_code != 200:
        return PreflightResult(
            name="mirror_disk_headroom",
            level="warn",
            message=f"mirror returned {resp.status_code}",
            detail={"status": resp.status_code, "body": resp.text[:200]},
        )
    body = resp.json()
    free = int(body.get("free_bytes") or 0)
    total = int(body.get("total_bytes") or 0)
    need = slot_image_size_bytes + safety_margin_bytes
    if free < need:
        return PreflightResult(
            name="mirror_disk_headroom",
            level="fail",
            message=(
                f"mirror has {free // (1024**3)} GiB free; "
                f"need {need // (1024**3)} GiB (slot image + margin)"
            ),
            detail={
                "free_bytes": free,
                "needed_bytes": need,
                "total_bytes": total,
                "path": body.get("path"),
            },
        )
    return PreflightResult(
        name="mirror_disk_headroom",
        level="ok",
        message=(f"mirror: {free // (1024**3)} GiB free " f"(need {need // (1024**3)} GiB)"),
        detail={
            "free_bytes": free,
            "needed_bytes": need,
            "total_bytes": total,
            "path": body.get("path"),
        },
    )


def check_version_path(
    *,
    target_version: str,
    current_version: str | None = None,
) -> PreflightResult:
    """Target is a valid forward jump from the current version.

    Rules:

    * Both versions must be CalVer-shaped (YYYY.MM.DD-N).
    * Target > current (no rollback through the upgrade flow — that's
      a separate slot-rollback button).
    * Skip-release: warn when the gap is > 90 days. We don't refuse
      because the appliance supports it via two rolling upgrades back
      to back, but the operator should know.
    """
    current = current_version or settings.version or "dev"
    if current == "dev":
        return PreflightResult(
            name="version_path",
            level="warn",
            message=(
                "current version is 'dev' — can't validate the upgrade path. "
                "Rolling upgrade from a dev build is unsupported; do a full "
                "redeploy from a tagged release first."
            ),
            detail={"current": current, "target": target_version},
        )
    cur_parts = _parse_calver(current)
    tgt_parts = _parse_calver(target_version)
    if cur_parts is None or tgt_parts is None:
        return PreflightResult(
            name="version_path",
            level="fail",
            message=(
                f"version parse failed (current={current!r}, "
                f"target={target_version!r}); both must match YYYY.MM.DD-N"
            ),
            detail={"current": current, "target": target_version},
        )
    if tgt_parts <= cur_parts:
        return PreflightResult(
            name="version_path",
            level="fail",
            message=(
                f"target {target_version} is not newer than current "
                f"{current}; rolling upgrade only moves forward — use "
                "slot rollback to revert"
            ),
            detail={"current": current, "target": target_version},
        )
    # Calendar gap in days.  Rough — counts calendar days only and
    # treats each month as 30 days for the warn threshold (we don't
    # need true date arithmetic for "is this a big jump").
    cur_days = cur_parts[0] * 365 + cur_parts[1] * 30 + cur_parts[2]
    tgt_days = tgt_parts[0] * 365 + tgt_parts[1] * 30 + tgt_parts[2]
    gap_days = tgt_days - cur_days
    if gap_days > 90:
        return PreflightResult(
            name="version_path",
            level="warn",
            message=(
                f"target is ~{gap_days} days newer than current "
                "(>90 d); consider an intermediate stop"
            ),
            detail={
                "current": current,
                "target": target_version,
                "gap_days": gap_days,
            },
        )
    return PreflightResult(
        name="version_path",
        level="ok",
        message=f"forward jump of ~{gap_days} days",
        detail={"current": current, "target": target_version, "gap_days": gap_days},
    )


def check_quorum() -> PreflightResult:
    """Cluster size is odd + ≥ 3 + every node Ready.

    Reads the kubeapi via the existing SA mount. On docker-compose
    deployments (no SA) we report ``ok`` with detail noting "single-
    instance shape" — single-node has no rolling upgrade.

    We only count nodes with our role label so a customer who's
    joined extra worker-only nodes for app workloads doesn't get
    misreported here. The label is the same one used to gate every
    appliance workload: ``spatium.io/role=appliance``.
    """
    from app.services.appliance import k8s  # noqa: PLC0415 — avoid top-level

    try:
        cfg = k8s.get_config()
    except k8s.KubeapiUnavailableError:
        cfg = None
    if cfg is None:
        return PreflightResult(
            name="quorum",
            level="ok",
            message="docker-compose / single-instance shape — no quorum check",
            detail={"deployment": "single_instance"},
        )
    # Pull node list directly via _request — we don't have a public
    # list_nodes helper today and don't want to grow one for one caller.
    from urllib.parse import quote  # noqa: PLC0415

    label = "spatium.io/role=appliance"
    path = f"/api/v1/nodes?labelSelector={quote(label)}"
    try:
        status, body = k8s._request("GET", path)  # noqa: SLF001
    except k8s.KubeapiUnavailableError as exc:
        return PreflightResult(
            name="quorum",
            level="warn",
            message=f"kubeapi unreachable: {exc}",
            detail={"error": str(exc)},
        )
    if status != 200:
        return PreflightResult(
            name="quorum",
            level="warn",
            message=f"kubeapi node list returned {status}",
            detail={"status": status},
        )
    import json  # noqa: PLC0415

    try:
        data = json.loads(body)
    except ValueError:
        return PreflightResult(
            name="quorum",
            level="warn",
            message="kubeapi node list JSON parse failed",
            detail={},
        )
    items = data.get("items") or []
    n = len(items)
    ready_count = 0
    not_ready: list[str] = []
    for node in items:
        name = (node.get("metadata") or {}).get("name", "<unknown>")
        conditions = (node.get("status") or {}).get("conditions") or []
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        if ready:
            ready_count += 1
        else:
            not_ready.append(name)
    if n < 3:
        return PreflightResult(
            name="quorum",
            level="fail",
            message=(
                f"only {n} appliance node(s) — rolling upgrade requires ≥ 3 "
                "(quorum-safe). Use single-node OS image upgrade instead."
            ),
            detail={"node_count": n},
        )
    if n % 2 == 0:
        return PreflightResult(
            name="quorum",
            level="fail",
            message=(
                f"even node count ({n}) — etcd quorum requires odd. "
                "Promote/demote to 3, 5, or 7 first."
            ),
            detail={"node_count": n},
        )
    if not_ready:
        return PreflightResult(
            name="quorum",
            level="fail",
            message=(
                f"{len(not_ready)} of {n} node(s) NotReady "
                f"({', '.join(not_ready)}); resolve before upgrading — "
                "rolling needs all healthy at start"
            ),
            detail={
                "node_count": n,
                "ready_count": ready_count,
                "not_ready": not_ready,
            },
        )
    return PreflightResult(
        name="quorum",
        level="ok",
        message=f"{n} appliance nodes, all Ready",
        detail={"node_count": n, "ready_count": ready_count},
    )


# ── Aggregator ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PreflightReport:
    """Aggregate of every check + a derived overall verdict."""

    target_version: str
    current_version: str
    overall: PreflightLevel  # worst level across results
    can_start: bool  # convenience: overall != "fail"
    results: list[PreflightResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_version": self.target_version,
            "current_version": self.current_version,
            "overall": self.overall,
            "can_start": self.can_start,
            "results": [asdict(r) for r in self.results],
        }


async def run_all(
    *,
    target_version: str,
    namespace: str | None = None,
) -> PreflightReport:
    """Run every check + return the aggregate report.

    Order doesn't matter (independent checks); we run them
    sequentially for now since none of them are slow. If any block
    of checks starts to dominate latency we can parallelise via
    ``asyncio.gather``.
    """
    results: list[PreflightResult] = [
        check_inflight_conflict(namespace=namespace),
        await check_replication_lag(),
        check_disk_headroom(),
        await check_mirror_disk_headroom(),
        check_version_path(target_version=target_version),
        check_quorum(),
        await check_kea_ha_version_skew(),
    ]
    levels = {r.level for r in results}
    if "fail" in levels:
        overall: PreflightLevel = "fail"
    elif "warn" in levels:
        overall = "warn"
    else:
        overall = "ok"
    return PreflightReport(
        target_version=target_version,
        current_version=settings.version or "dev",
        overall=overall,
        can_start=overall != "fail",
        results=results,
    )


# Kea 3.0's HA hook cannot exchange lease updates with a peer older than this.
# 3.0 added the "released" lease state (value 3) to the updates partners send
# each other; a pre-2.7 peer rejects them outright. Upstream is explicit that
# every HA member must be upgraded at the same time.
_KEA_HA_MIN_COMPATIBLE_MAJOR = 3


def _kea_major(version: str | None) -> int | None:
    """Major version from a Kea version string ("3.0.3" → 3). None if unknown."""
    if not version:
        return None
    head = version.strip().split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


async def check_kea_ha_version_skew() -> PreflightResult:
    """Warn when a rolling upgrade will disrupt a Kea HA pair (#637).

    The #296 orchestrator upgrades nodes **one at a time** — cordon, drain, swap
    slot, reboot, uncordon, next. That is exactly the sequence Kea 3.0's HA hook
    cannot survive when the pair starts on 2.6: mid-run one member is on 3.0 and
    its partner is still on 2.6, they reject each other's lease updates, and the
    pair falls out of sync until BOTH have crossed. There is no fix inside the
    orchestrator — the incompatibility is in Kea's wire protocol, and ISC's
    guidance is to upgrade every HA member together. So the honest thing is to
    tell the operator before they press Start.

    Two pieces of scoping matter, and getting either wrong turns this check into
    a liar that blocks unrelated work:

    * **Only appliance nodes.** The orchestrator only ever touches appliance
      nodes; docker / k8s DHCP agents upgrade through the manual copy-paste path
      and are never cordoned, drained or slot-swapped by this run. Counting them
      would let two unrelated docker containers veto an appliance-cluster
      upgrade. ``deployment_kind`` is matched strictly against ``appliance`` (an
      ``appliance`` OR ``NULL`` fallback would lie about a row that has not
      checked in yet — same convention as the Upgrade / Reboot affordances).
    * **Only *real* HA pairs.** HA is not "≥ 2 Kea members" — that is half the
      condition. ``_resolve_failover`` in ``services/dhcp/config_bundle.py``
      renders the HA hook only when there are ≥ 2 Kea members AND every one of
      them carries a non-empty ``ha_peer_url``; without a URL a peer cannot be
      reached for heartbeats or lease updates, so no HA relationship exists and
      there is nothing for an upgrade to disrupt. This check must use the same
      predicate or it invents HA pairs that the renderer never built.

    **This check never returns ``fail``.** A ``fail`` sets ``can_start=False``,
    and blocking the upgrade would be exactly backwards: if a pair is *already*
    split across Kea majors its HA is broken right now, and completing the
    rolling upgrade is what converges both members onto one version. Refusing to
    start would strand the operator in the broken state. So an already-mixed pair
    is a loud ``warn`` that says "proceeding will fix this", not a gate.
    """
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(text("""
                            SELECT g.name         AS group_name,
                                   s.name         AS server_name,
                                   s.kea_version  AS kea_version,
                                   s.ha_peer_url  AS ha_peer_url
                            FROM dhcp_server_group g
                            JOIN dhcp_server s ON s.server_group_id = g.id
                            WHERE s.driver = 'kea'
                              AND s.deployment_kind = 'appliance'
                            ORDER BY g.name, s.name
                            """))).mappings().all()
    except Exception as e:  # pragma: no cover - DB unavailable is its own signal
        logger.warning("preflight_kea_ha_skew_query_failed", error=str(e))
        return PreflightResult(
            name="kea_ha_version_skew",
            level="warn",
            message="Could not determine Kea versions — verify HA members manually.",
            detail={"error": str(e)},
        )

    by_group: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_group.setdefault(r["group_name"], []).append(dict(r))

    # Mirror _resolve_failover exactly: ≥ 2 Kea members AND every member has a
    # peer URL. Anything else renders no HA hook, so there is no HA to disrupt.
    ha_groups = {
        name: members
        for name, members in by_group.items()
        if len(members) >= 2 and all(m["ha_peer_url"] for m in members)
    }
    if not ha_groups:
        return PreflightResult(
            name="kea_ha_version_skew",
            level="ok",
            message="No Kea HA pairs on appliance nodes — no same-window upgrade constraint.",
            detail={"ha_groups": 0},
        )

    # Classification is mutually exclusive, worst-first, so a group is reported
    # under exactly one heading and the structured detail can't contradict itself.
    mixed: list[str] = []
    pre_3: list[str] = []
    unknown: list[str] = []
    groups: list[dict[str, Any]] = []
    for name, members in ha_groups.items():
        majors = {_kea_major(m["kea_version"]) for m in members}
        known = {m for m in majors if m is not None}
        if len(known) > 1:
            mixed.append(name)
        elif None in majors:
            # At least one member has never reported. Unknown, never "old" —
            # even if a sibling reports a pre-3.0 version, we can't say what the
            # silent one runs, so "unknown" is the honest heading.
            unknown.append(name)
        elif known and next(iter(known)) < _KEA_HA_MIN_COMPATIBLE_MAJOR:
            pre_3.append(name)
        groups.append(
            {
                "group": name,
                "members": [
                    {"server": m["server_name"], "kea_version": m["kea_version"]} for m in members
                ],
            }
        )

    detail: dict[str, Any] = {
        "ha_groups": len(ha_groups),
        "mixed_major": mixed,
        "pre_3_0": pre_3,
        "unknown_version": unknown,
        "groups": groups,
    }

    if mixed:
        return PreflightResult(
            name="kea_ha_version_skew",
            level="warn",
            message=(
                f"Kea HA {'pairs' if len(mixed) > 1 else 'pair'} {', '.join(mixed)} "
                "already span different Kea major versions, so HA lease sync is "
                "broken right now. Completing this upgrade is what converges every "
                "member onto one version — expect HA to stay degraded until it finishes."
            ),
            detail=detail,
        )
    if pre_3:
        return PreflightResult(
            name="kea_ha_version_skew",
            level="warn",
            message=(
                f"Kea HA {'pairs' if len(pre_3) > 1 else 'pair'} {', '.join(pre_3)} "
                "run Kea < 3.0. This upgrade moves nodes one at a time, and Kea 3.0's "
                "HA hook cannot exchange lease updates with a pre-2.7 peer — so the "
                "pair will fall out of sync until every member has crossed. DHCP keeps "
                "serving throughout; only HA replication between the peers is affected."
            ),
            detail=detail,
        )
    if unknown:
        return PreflightResult(
            name="kea_ha_version_skew",
            level="warn",
            message=(
                f"Kea version unknown for {'groups' if len(unknown) > 1 else 'group'} "
                f"{', '.join(unknown)} (agent has not reported it yet). If those members "
                "are on Kea < 3.0, HA will be degraded mid-upgrade."
            ),
            detail=detail,
        )
    return PreflightResult(
        name="kea_ha_version_skew",
        level="ok",
        message=f"All {len(ha_groups)} Kea HA group(s) already on Kea ≥ 3.0.",
        detail=detail,
    )
