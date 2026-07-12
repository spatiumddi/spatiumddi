"""Minimal Kubernetes API client for the k3s lifecycle path (#183).

Mirrors the ``docker_api.py`` shape: direct HTTP calls against the
local k3s apiserver, no shelling to ``kubectl``. Goes through the
same in-cluster service-account token + CA bundle the k8s Python
client would use, just without pulling in the full ``kubernetes``
library (which has ~30 transitive deps + significant import-time
cost).

The supervisor runs as an in-cluster pod once Phase 3 lands; the
service-account auto-mount at /var/run/secrets/kubernetes.io/
serviceaccount/{token,ca.crt} provides everything. When the
supervisor is launched outside of a pod (legacy compose path,
local dev), the env loader falls back to /etc/rancher/k3s/k3s.yaml
parsed for the operator-equivalent admin context.

Failure modes match docker_api: any error returns an empty / sentinel
result + a structlog warning. The supervisor's lifecycle module
converts those to a ``failed`` state so heartbeats surface them.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog

log = structlog.get_logger(__name__)

# Service-account-mount paths. Always present when the supervisor
# runs as an in-cluster pod; absent in legacy / dev / before-pod
# contexts.
_SA_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_SA_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
_SA_NAMESPACE_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")

# In-cluster apiserver — k8s exposes itself at this fixed env-derived
# host:port from inside any pod via the service-account auto-config.
_INCLUSTER_HOST_ENV = "KUBERNETES_SERVICE_HOST"
_INCLUSTER_PORT_ENV = "KUBERNETES_SERVICE_PORT"

# Fallback when the supervisor isn't running in a pod yet — read the
# operator's admin kubeconfig from the host bind mount.
_HOST_KUBECONFIG_PATH = Path("/etc/rancher/k3s/k3s.yaml")


@dataclass(frozen=True)
class KubeConfig:
    """Resolved connection params for the k3s apiserver.

    Three possible sources, in priority order:
      1. **In-cluster** — service-account token + ca.crt mounted by
         the kubelet. The standard "I'm a pod" path.
      2. **Host kubeconfig** — /etc/rancher/k3s/k3s.yaml mounted via
         hostPath on the supervisor pod (Phase 1 default). Used until
         the supervisor migrates to in-cluster auth.
      3. **None** — k3s isn't running here. Callers fall back to the
         docker-compose path.
    """

    host: str
    port: int
    token: str | None
    ca_path: str | None
    namespace: str
    # Mark whether this was an in-cluster resolution. Phase 4 widens
    # the kubeapi bind; for now host-kubeconfig means "we ARE on this
    # appliance + k3s is up but we're not yet a pod".
    in_cluster: bool = False


@dataclass
class PodStatus:
    """Trimmed kubeapi Pod state for the watchdog. Same shape the
    docker_api.list_running_containers result feeds into watchdog
    today — let's pretend the heartbeat-side renderer doesn't care
    which runtime answered."""

    name: str
    namespace: str
    status: str  # Pending / Running / Succeeded / Failed / Unknown
    container_statuses: list[dict[str, Any]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


def _resolve_config() -> KubeConfig | None:
    """Pick the right kubeapi connection params for the current
    process. Returns ``None`` when neither in-cluster nor a host
    kubeconfig is available — the caller treats that as "k3s isn't
    here, fall back to docker compose"."""
    # Path 1: in-cluster pod with auto-mounted service account.
    host = os.environ.get(_INCLUSTER_HOST_ENV)
    port_s = os.environ.get(_INCLUSTER_PORT_ENV)
    if host and port_s and _SA_TOKEN_PATH.exists() and _SA_CA_PATH.exists():
        try:
            token = _SA_TOKEN_PATH.read_text(encoding="utf-8").strip()
            ns = (
                _SA_NAMESPACE_PATH.read_text(encoding="utf-8").strip()
                if _SA_NAMESPACE_PATH.exists()
                else "default"
            )
            return KubeConfig(
                host=host,
                port=int(port_s),
                token=token,
                ca_path=str(_SA_CA_PATH),
                namespace=ns,
                in_cluster=True,
            )
        except (OSError, ValueError) as exc:
            log.warning("supervisor.k8s_api.sa_read_failed", error=str(exc))

    # Path 2: host kubeconfig (operator-admin auth via the kubelet's
    # generated cert). Parse minimally — we only need host:port +
    # the embedded client cert/key for TLS.
    if _HOST_KUBECONFIG_PATH.exists():
        try:
            return _parse_host_kubeconfig(_HOST_KUBECONFIG_PATH)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("supervisor.k8s_api.kubeconfig_parse_failed", error=str(exc))
    return None


def _parse_host_kubeconfig(path: Path) -> KubeConfig:
    """Read host kubeconfig at ``path`` and return a KubeConfig.

    Intentionally minimal — only extracts ``cluster.server`` (host +
    port) and ``user.token`` if present. Client-cert auth from the
    standard k3s kubeconfig isn't supported in this minimal client
    (would require parsing PEM + driving SSLContext mTLS — defer to
    the in-cluster path which uses service-account bearer tokens).
    Falls through to return a KubeConfig with token=None; callers
    that hit a 401 should log + return empty.

    Phase 4 widens this: once the supervisor's mTLS cert doubles as
    a k8s client cert, we'll thread the SupervisorIdentity's
    private key in here. Phase 3 sticks with the in-cluster SA path
    once the supervisor is podified — this branch is only used
    pre-podification (legacy compose where someone still wants
    introspection).
    """
    import yaml  # noqa: PLC0415 — lazy import; only on host-kubeconfig path.

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    contexts = {c["name"]: c["context"] for c in data.get("contexts") or []}
    current = data.get("current-context")
    if current not in contexts:
        raise KeyError(f"current-context {current!r} not in kubeconfig contexts")
    ctx = contexts[current]
    clusters = {c["name"]: c["cluster"] for c in data.get("clusters") or []}
    users = {u["name"]: u["user"] for u in data.get("users") or []}
    cluster = clusters[ctx["cluster"]]
    user = users[ctx["user"]]

    server = cluster["server"]
    # k3s default: https://127.0.0.1:6443
    if server.startswith("https://"):
        rest = server[len("https://") :]
    elif server.startswith("http://"):
        rest = server[len("http://") :]
    else:
        rest = server
    if ":" in rest:
        host_part, port_part = rest.rsplit(":", 1)
        port = int(port_part.split("/")[0])
    else:
        host_part = rest.split("/")[0]
        port = 6443

    # Host kubeconfig path doesn't ship a CA path we can use directly;
    # the CA bytes are inline (base64-encoded). Write a CA-bundle file
    # once at process start under the supervisor's own state dir —
    # NOT /tmp, which would be a predictable filename on a world-
    # writable dir (issue #235: symlink-race vector since the
    # supervisor runs privileged). ``O_NOFOLLOW`` defends against a
    # symlink even within state_dir on the off chance another
    # writer can drop one there.
    ca_path: str | None = None
    ca_b64 = cluster.get("certificate-authority-data")
    if ca_b64:
        import base64  # noqa: PLC0415

        state_dir = Path(os.environ.get("STATE_DIR", "/var/lib/spatium-supervisor"))
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            target = state_dir / ".k3s-ca.crt"
            # Open with O_NOFOLLOW so a pre-existing symlink at the
            # target path isn't followed. O_CREAT + O_TRUNC make the
            # write idempotent across process restarts.
            fd = os.open(
                str(target),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, base64.b64decode(ca_b64))
            finally:
                os.close(fd)
            ca_path = str(target)
        except OSError as exc:
            log.warning("supervisor.k8s_api.ca_write_failed", error=str(exc))

    return KubeConfig(
        host=host_part,
        port=port,
        token=user.get("token"),
        ca_path=ca_path,
        namespace="default",
        in_cluster=False,
    )


# Cache the resolved config for the supervisor's lifetime. A pod
# restart re-resolves (which is what we want — picks up rotated
# service-account tokens).
_config_cache: KubeConfig | None = None
_config_resolved: bool = False


def get_config() -> KubeConfig | None:
    """Resolve kubeapi connection params once + cache for the
    supervisor's lifetime."""
    global _config_cache, _config_resolved
    if not _config_resolved:
        _config_cache = _resolve_config()
        _config_resolved = True
    return _config_cache


def _ssl_context(ca_path: str | None) -> ssl.SSLContext:
    """Build an SSLContext that verifies the kubeapi server cert.

    Issue #233 — refuses to connect when no CA path is resolvable.
    The pre-#233 fallback silently dropped to ``verify_mode=CERT_NONE``
    with only a log.warning; the supervisor runs privileged and even
    a loopback channel is MITM-able by a tampered cni / sidecar.
    Operators on dev boxes pointed at a self-signed kubeapi can
    explicitly opt out by setting ``SPATIUM_INSECURE_SKIP_TLS_VERIFY=1``
    in the supervisor env.
    """
    ctx = ssl.create_default_context()
    if ca_path:
        ctx.load_verify_locations(cafile=ca_path)
        return ctx
    if os.environ.get("SPATIUM_INSECURE_SKIP_TLS_VERIFY") == "1":
        log.warning("supervisor.k8s_api.ssl_unverified_opt_in")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    raise RuntimeError(
        "k8s_api: refusing to build TLS context — no CA path resolved "
        "and SPATIUM_INSECURE_SKIP_TLS_VERIFY is not set. Inspect the "
        "host kubeconfig at /etc/rancher/k3s/k3s.yaml for a missing "
        "``certificate-authority-data:`` field, or set the env var "
        "explicitly for a dev / self-signed setup."
    )


def _request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    """Issue a request to the kubeapi server. Returns
    ``(status_code, response_body)``. Raises ``RuntimeError`` on
    transport-level failures (DNS, connect timeout, TLS handshake)."""
    cfg = get_config()
    if cfg is None:
        raise RuntimeError("k3s kubeapi not reachable (no config resolved)")
    conn = http.client.HTTPSConnection(
        cfg.host, cfg.port, timeout=timeout, context=_ssl_context(cfg.ca_path)
    )
    try:
        headers = {"Host": cfg.host, "Accept": "application/json"}
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        if content_type:
            headers["Content-Type"] = content_type
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, socket.timeout, ssl.SSLError) as exc:
        raise RuntimeError(f"kubeapi {method} {path}: {exc}") from exc
    finally:
        conn.close()


def check_kubeapi_ready(timeout: float = 2.0) -> bool:
    """Probe ``/readyz`` — returns True iff kubeapi reports OK.

    Used by the watchdog (Phase 3) to decide whether to take action.
    Sub-2s timeout so a wedged apiserver doesn't stall the heartbeat
    loop."""
    try:
        status, body = _request("GET", "/readyz", timeout=timeout)
    except RuntimeError as exc:
        log.warning("supervisor.k8s_api.readyz_failed", error=str(exc))
        return False
    return status == 200 and body.strip() == b"ok"


def list_pods(
    namespace: str = "spatium", label_selector: str | None = None
) -> list[PodStatus]:
    """List pods in ``namespace``, optionally filtered by
    ``label_selector`` (standard kubeapi label-selector syntax).

    Returns ``[]`` on any error — same fail-soft semantics as
    ``docker_api.list_running_containers``."""
    path = f"/api/v1/namespaces/{quote(namespace)}/pods"
    if label_selector:
        path += f"?labelSelector={quote(label_selector)}"
    try:
        status, body = _request("GET", path)
    except RuntimeError as exc:
        log.warning("supervisor.k8s_api.list_pods_failed", error=str(exc))
        return []
    if status != 200:
        log.warning(
            "supervisor.k8s_api.list_pods_status", status=status, body=body[:200]
        )
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning("supervisor.k8s_api.list_pods_decode_failed", error=str(exc))
        return []
    out: list[PodStatus] = []
    for item in data.get("items") or []:
        meta = item.get("metadata") or {}
        status_block = item.get("status") or {}
        out.append(
            PodStatus(
                name=meta.get("name") or "",
                namespace=meta.get("namespace") or namespace,
                status=status_block.get("phase") or "Unknown",
                container_statuses=status_block.get("containerStatuses") or [],
                labels=meta.get("labels") or {},
            )
        )
    return out


def apply_helmchart(
    name: str,
    *,
    chart_content_b64: str,
    values: dict[str, Any],
    target_namespace: str = "spatium",
    chart_namespace: str = "kube-system",
) -> tuple[bool, str | None]:
    """Create or update a ``HelmChart`` custom resource (k3s's
    built-in helm-controller picks it up + runs helm upgrade).

    ``chart_content_b64`` is the base64-encoded chart tarball (output
    of ``helm package`` then ``base64``). Air-gap-friendly: no chart
    repo lookup, no registry call — the entire chart ships in the
    HelmChart CR body.

    ``values`` is rendered as YAML in ``spec.valuesContent`` so the
    chart sees the operator's per-role flags + the supervisor's
    derived control-plane URL.

    Returns ``(success, error_string)``. Same idempotent shape as
    apply_role_assignment in the compose path: re-applying with the
    same content is a no-op for k3s's helm-controller (Helm tracks
    revision diffs internally).
    """
    import yaml  # noqa: PLC0415 — lazy; only on apply path.

    values_yaml = yaml.safe_dump(values, default_flow_style=False, sort_keys=False)
    body = {
        "apiVersion": "helm.cattle.io/v1",
        "kind": "HelmChart",
        "metadata": {"name": name, "namespace": chart_namespace},
        "spec": {
            "chartContent": chart_content_b64,
            "targetNamespace": target_namespace,
            "createNamespace": True,
            "valuesContent": values_yaml,
        },
    }
    payload = json.dumps(body).encode("utf-8")
    # Server-side apply with field manager — k3s's helm-controller is
    # the field manager for HelmChart objects on the same fields, so
    # we ack co-ownership.
    path = (
        f"/apis/helm.cattle.io/v1/namespaces/{quote(chart_namespace)}"
        f"/helmcharts/{quote(name)}"
        "?fieldManager=spatium-supervisor&force=true"
    )
    try:
        status, resp = _request(
            "PATCH",
            path,
            body=payload,
            content_type="application/apply-patch+yaml",
        )
    except RuntimeError as exc:
        return False, str(exc)
    if status in (200, 201):
        return True, None
    return False, f"kubeapi status {status}: {resp[:200]!r}"


def delete_helmchart(
    name: str, chart_namespace: str = "kube-system"
) -> tuple[bool, str | None]:
    """Delete a HelmChart CR. k3s's helm-controller catches the
    delete event and runs ``helm uninstall``. Idempotent — deleting
    a non-existent CR returns success."""
    path = f"/apis/helm.cattle.io/v1/namespaces/{quote(chart_namespace)}/helmcharts/{quote(name)}"
    try:
        status, resp = _request("DELETE", path)
    except RuntimeError as exc:
        return False, str(exc)
    if status in (200, 202, 404):
        return True, None
    return False, f"kubeapi status {status}: {resp[:200]!r}"


def list_etcd_snapshots() -> list[dict[str, Any]]:
    """List recoverable etcd snapshots from the k3s ``ETCDSnapshotFile``
    cluster-scoped CRs (#272 Phase 9b).

    k3s ≥ 1.26 materialises one ``etcdsnapshotfile.k3s.cattle.io`` object
    per on-disk (and S3) snapshot — the same source ``k3s etcd-snapshot
    list`` reads — so the supervisor reads them over the kubeapi it
    already talks to, with NO host ``k3s`` binary or etcd access needed.

    Returns ``[{name, location, node_name, size, created_at}]`` sorted
    newest-first, or ``[]`` on any error (older k3s without the CRD, a
    kubeapi blip, or a non-seed node whose read 403s)."""
    path = "/apis/k3s.cattle.io/v1/etcdsnapshotfiles"
    try:
        status, resp = _request("GET", path)
    except RuntimeError:
        return []
    if status != 200:
        # #389 — a 403 here is the common cause of an empty Fleet → etcd
        # snapshots list: the supervisor ServiceAccount needs a
        # ``k3s.cattle.io/etcdsnapshotfiles`` read grant
        # (supervisor-rbac.yaml). Log it so a missing grant is
        # self-diagnosing instead of an invisible empty list. A 404
        # (older k3s without the CRD) is benign → debug, not warning.
        emit = log.warning if status == 403 else log.debug
        emit(
            "supervisor.k8s_api.list_etcd_snapshots_status",
            status=status,
            body=resp[:200],
        )
        return []
    try:
        items = (json.loads(resp) or {}).get("items") or []
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        spec = it.get("spec") or {}
        st = it.get("status") or {}
        out.append(
            {
                "name": spec.get("snapshotName")
                or (it.get("metadata") or {}).get("name")
                or "",
                "location": spec.get("location") or "",
                "node_name": spec.get("nodeName") or "",
                "size": st.get("size"),
                "created_at": st.get("creationTime"),
            }
        )
    # Newest-first by created_at (ISO 8601 sorts lexically); blanks last.
    out.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return out


def delete_node(name: str) -> tuple[bool, str | None]:
    """Delete a k8s Node. On k3s, deleting a server Node object makes
    the cluster drop its etcd member — so this is how a dead
    control-plane member is evicted (#272 Phase 9 dead-node
    replacement). Only the seed runs it (it holds the admin kubeconfig).
    Idempotent — a 404 (already gone) counts as success."""
    path = f"/api/v1/nodes/{quote(name)}"
    try:
        status, resp = _request("DELETE", path)
    except RuntimeError as exc:
        return False, str(exc)
    if status in (200, 202, 404):
        return True, None
    return False, f"kubeapi status {status}: {resp[:200]!r}"


def reclaim_stranded_redis_storage(
    node: str, namespace: str = "spatium"
) -> tuple[list[str], str | None]:
    """Delete Redis PVCs (and their Pending consumer pods) stranded on a
    just-deleted node — returns (reclaimed_pvc_names, error).

    #590 — local-path PVs are node-affine, so evicting a dead node
    permanently strands every ReadWriteOnce PVC provisioned on it: the
    StatefulSet's replacement pod references the old claim and sits
    ``Pending`` forever ("volume node affinity conflict"). For the
    Sentinel Redis that is not cosmetic — the missing replica's sentinel
    silently drops the quorum from 3 to 2, and the NEXT node loss leaves
    one lone sentinel that can never authorize a failover: the master is
    stranded, ``sentinel://`` clients never resolve a new one, and the
    appliance API is down cluster-wide (observed live 2026-07-12; the
    chart README documented the manual PVC-delete repair — an appliance
    must do it itself).

    Redis here is cache + Celery broker; Postgres is the store of record,
    so the data is expendable and the replica resyncs from the master.
    Deliberately restricted to claims with ``-redis-`` in the name: CNPG
    manages (deletes + recreates) its own instance PVCs, and anything
    else is not ours to reap. The PVC goes first (pvc-protection holds it
    until its pod is gone), then the pod — the StatefulSet then recreates
    both and the provisioner lands the new PV on a live node."""
    base = f"/api/v1/namespaces/{quote(namespace)}"
    try:
        status, resp = _request("GET", f"{base}/persistentvolumeclaims")
    except RuntimeError as exc:
        return [], str(exc)
    if status != 200:
        return [], f"kubeapi status {status}: {resp[:200]!r}"
    try:
        items = json.loads(resp).get("items", [])
    except ValueError:
        return [], "unparseable PVC list"
    reclaimed: list[str] = []
    for pvc in items:
        meta = pvc.get("metadata") or {}
        name = str(meta.get("name") or "")
        anns = meta.get("annotations") or {}
        if "-redis-" not in name:
            continue
        if anns.get("volume.kubernetes.io/selected-node") != node:
            continue
        try:
            status, resp = _request(
                "DELETE", f"{base}/persistentvolumeclaims/{quote(name)}")
        except RuntimeError as exc:
            return reclaimed, str(exc)
        if status not in (200, 202, 404):
            return reclaimed, f"kubeapi status {status}: {resp[:200]!r}"
        # volumeClaimTemplate name is the prefix: data-<pod-name>. Delete
        # the Pending pod so the StatefulSet recreates it against a fresh
        # claim (an existing pod keeps referencing the deleted PVC).
        pod = name.partition("-")[2]
        if pod:
            try:
                _request("DELETE", f"{base}/pods/{quote(pod)}")
            except RuntimeError:
                pass  # pod may not exist; the PVC reclaim is what matters
        reclaimed.append(name)
    return reclaimed, None


# #272 — durable control-plane state via k3s HelmChartConfig.
#
# The seed supervisor reflects cluster state (control-plane member count,
# MetalLB pool + VIP) onto the helm releases. Patching the HelmChart CR's
# valuesContent directly is NOT reboot-safe: the HelmChart is a k3s
# auto-deploy manifest, so k3s re-applies the on-disk manifest (firstboot
# defaults: cp-size=1, metallb off, VIP "") to the CR on every k3s restart
# (i.e. every node reboot), clobbering the patch. A single seed reboot
# then scaled the control plane to 1 replica + dropped MetalLB/the VIP.
#
# A HelmChartConfig is the k3s-native fix: a SEPARATE CR (not derived from
# any manifest, so the deploy controller never touches it) whose
# valuesContent helm-controller MERGES on top of the same-named
# HelmChart's. We write the supervisor-owned overrides there → they
# survive the manifest re-apply. The firstboot HelmChart keeps the
# defaults as the floor.


def _helmchartconfig_upsert(
    name: str, values_yaml: str, *, namespace: str = "kube-system"
) -> tuple[bool, str | None]:
    """Create-or-update the HelmChartConfig ``name`` so its
    ``spec.valuesContent`` equals ``values_yaml``. Idempotent — returns
    ``(False, None)`` when already current. helm-controller merges this
    on top of the same-named HelmChart's values, and it survives k3s
    manifest re-apply on restart (unlike a HelmChart CR patch)."""
    base = f"/apis/helm.cattle.io/v1/namespaces/{quote(namespace)}/helmchartconfigs"
    path = f"{base}/{quote(name)}"
    try:
        status, resp = _request("GET", path)
    except RuntimeError as exc:
        return False, str(exc)
    if status == 200:
        try:
            cur = (json.loads(resp).get("spec") or {}).get("valuesContent") or ""
        except (json.JSONDecodeError, ValueError):
            cur = None
        if cur == values_yaml:
            return False, None
        patch = json.dumps({"spec": {"valuesContent": values_yaml}}).encode("utf-8")
        try:
            st, rb = _request(
                "PATCH", path, body=patch, content_type="application/merge-patch+json"
            )
        except RuntimeError as exc:
            return False, str(exc)
        return (st in (200, 201)), (
            None if st in (200, 201) else f"PATCH status {st}: {rb[:200]!r}"
        )
    if status != 404:
        return False, f"kubeapi GET status {status}"
    body = json.dumps(
        {
            "apiVersion": "helm.cattle.io/v1",
            "kind": "HelmChartConfig",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"valuesContent": values_yaml},
        }
    ).encode("utf-8")
    try:
        st, rb = _request("POST", base, body=body, content_type="application/json")
    except RuntimeError as exc:
        return False, str(exc)
    return (st in (200, 201)), (
        None if st in (200, 201) else f"POST status {st}: {rb[:200]!r}"
    )


def apply_control_plane_overrides(
    cp_size: int,
    control_plane_vip: str,
    web_ui_allowed_cidrs: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Durably set the spatium-control overrides: api / frontend / worker
    replicas + CNPG instances + redis sentinel replicas = ``cp_size``,
    plus the frontend control-plane VIP. Written to the spatium-control
    HelmChartConfig so it survives a k3s restart (#272).

    #285 Phase 6 — ``web_ui_allowed_cidrs`` (empty = open) also lands on the
    frontend as ``loadBalancerSourceRanges``, so the MetalLB VIP path is
    source-scoped by the same setting that scopes the per-node hostPort door
    via nftables. Belt (VIP) + braces (hostPort) from one operator control."""
    if cp_size < 1:
        return False, "cp_size < 1"
    vip = (control_plane_vip or "").strip()
    # Flow-style JSON list is valid YAML; empty list = open (field omitted by
    # the chart template). The supervisor already validates these CIDRs at the
    # control plane, but they arrive here as plain strings — json.dumps keeps
    # them quoted so a malformed entry can't break the YAML overlay.
    src_json = json.dumps(
        [c.strip() for c in (web_ui_allowed_cidrs or []) if c and c.strip()]
    )
    # #590 — pin api/frontend/worker to one replica per control-plane node,
    # and evict them from a dead node in seconds rather than the k8s default
    # 300 s. ``replicas`` here IS the node count, so hard
    # (requiredDuringScheduling) anti-affinity is exactly right, and the
    # chart no-ops it below 2 replicas.
    #
    # Written on every promote/demote rather than relying on the
    # firstboot-rendered HelmChart values, because firstboot only runs on a
    # FRESH install — an appliance that A/B-upgrades into this fix would
    # otherwise keep the old un-spread values forever. Without it a promote
    # could stack every api pod on the seed (they schedule while the new
    # members may not yet be labelled), so losing the seed left no ready api
    # anywhere and every node answered 502.
    #
    # The tolerations are NOT a chart default: pinning both taint keys
    # suppresses the DefaultTolerationSeconds admission plugin, which a
    # BYO-Kubernetes install still wants. They belong to the appliance,
    # where the control-plane node count is fixed.
    fast_evict = (
        "  tolerations:\n"
        "    - key: node.kubernetes.io/unreachable\n"
        "      operator: Exists\n"
        "      effect: NoExecute\n"
        "      tolerationSeconds: 20\n"
        "    - key: node.kubernetes.io/not-ready\n"
        "      operator: Exists\n"
        "      effect: NoExecute\n"
        "      tolerationSeconds: 20\n"
    )
    values = (
        f"api:\n  replicas: {cp_size}\n  podAntiAffinity: hard\n{fast_evict}"
        f"frontend:\n  replicas: {cp_size}\n  podAntiAffinity: hard\n{fast_evict}"
        f'  controlPlaneVIP: "{vip}"\n'
        f"  loadBalancerSourceRanges: {src_json}\n"
        f"worker:\n  replicas: {cp_size}\n  podAntiAffinity: hard\n{fast_evict}"
        f"postgresql:\n  cnpg:\n    instances: {cp_size}\n"
        f"redis:\n  sentinel:\n    replicas: {cp_size}\n"
    )
    return _helmchartconfig_upsert("spatium-control", values)


def apply_metallb_overrides(
    *,
    metallb_enabled: bool,
    pool_addresses: list[str],
    bgp_enabled: bool = False,
    bgp_peers: list[dict] | None = None,
    bgp_advertisements: list[dict] | None = None,
) -> tuple[bool, str | None]:
    """Durably set the MetalLB overrides (L2 pool + BGP mode) on the
    spatium-metallb HelmChartConfig (#272 / #566).

    MetalLB moved out of the spatium-bootstrap chart into its own
    spatium-metallb chart (deployed in the metallb-system namespace), so
    the override targets that chart's HelmChartConfig now. The value
    paths are unchanged (``metallb.enabled`` + ``metallb.ipPool
    .addresses``) — the wrapper chart reads the same keys.

    ``bgp_peers`` / ``bgp_advertisements`` are plain dicts in the
    snake_case shape the PlatformSettings JSONB columns store
    (``my_asn`` / ``peer_asn`` / ``peer_address`` / ``peer_port`` /
    ``hold_time`` and ``ip_address_pools`` / ``communities`` /
    ``aggregation_length``) — translated here to the chart's camelCase
    BGPPeer/BGPAdvertisement CR field names (myASN/peerASN/peerAddress/
    peerPort/holdTime, ipAddressPools/communities/aggregationLength).
    ``frrk8s.enabled`` is driven by the SAME ``bgp_enabled`` flag as
    ``bgp.enabled`` — the chart's speaker/controller only wire up to
    frr-k8s when frrk8s.enabled is true, and BGP peers/advertisements
    are inert without it. ``speaker.frr.enabled`` is NEVER written here
    — it stays the chart's baked ``false`` (mutually exclusive with
    frrk8s.enabled; the chart's own template hard-``fail``s if both are
    true).

    MUST render every MetalLB-related key in ONE combined body —
    ``_helmchartconfig_upsert`` replaces the whole valuesContent string,
    so calling this (or a sibling function targeting the same
    HelmChartConfig) twice per tick would have the second call blank
    out the first's keys."""
    pool_json = json.dumps([a.strip() for a in pool_addresses if a and a.strip()])

    def _peer(p: dict) -> dict:
        out: dict = {
            "myASN": p["my_asn"],
            "peerASN": p["peer_asn"],
            "peerAddress": p["peer_address"],
        }
        if p.get("peer_port"):
            out["peerPort"] = p["peer_port"]
        if p.get("hold_time"):
            out["holdTime"] = p["hold_time"]
        return out

    def _adv(a: dict) -> dict:
        out: dict = {
            "ipAddressPools": a.get("ip_address_pools") or ["spatium-control-plane"]
        }
        if a.get("communities"):
            out["communities"] = a["communities"]
        if a.get("aggregation_length"):
            out["aggregationLength"] = a["aggregation_length"]
        return out

    peers_json = json.dumps([_peer(p) for p in (bgp_peers or [])])
    adv_json = json.dumps([_adv(a) for a in (bgp_advertisements or [])])
    values = (
        f"metallb:\n  enabled: {'true' if metallb_enabled else 'false'}\n"
        f"  ipPool:\n    addresses: {pool_json}\n"
        f"  frrk8s:\n    enabled: {'true' if bgp_enabled else 'false'}\n"
        f"  bgp:\n    enabled: {'true' if bgp_enabled else 'false'}\n"
        f"    peers: {peers_json}\n"
        f"    advertisements: {adv_json}\n"
    )
    return _helmchartconfig_upsert("spatium-metallb", values)


def apply_dataplane_vip_overrides(
    *, dns_vip: str, dhcp_relay_vip: str
) -> tuple[bool, str | None]:
    """Durably set the data-plane resolver VIPs (#272 Phase 10) on the
    spatiumddi-appliance HelmChartConfig.

    ``dns_vip`` (non-empty) flips the bind9 / powerdns DaemonSets OFF
    hostNetwork and behind a single L2 LoadBalancer Service at the VIP
    (``dns.useMetalLBVIP`` + ``dns.vip``); empty keeps hostNetwork :53.
    ``dhcp_relay_vip`` adds the relay→server LoadBalancer Service on :67
    (``dhcpKea.relayVIP``) without touching Kea's hostNetwork broadcast
    path; empty renders no relay Service.

    Written to the HelmChartConfig (not the HelmChart CR) so it survives
    a k3s restart's manifest re-apply, exactly like the cp-size + VIP
    overrides. helm-controller merges it on top of the role-assignment
    values the supervisor PATCHes onto the same-named HelmChart, so this
    overlay only carries the VIP keys and never fights for ownership of
    the per-role ``enabled`` flags. Idempotent — only writes on change."""
    dns_vip = (dns_vip or "").strip()
    relay_vip = (dhcp_relay_vip or "").strip()
    values = (
        f"dns:\n  useMetalLBVIP: {'true' if dns_vip else 'false'}\n"
        f'  vip: "{dns_vip}"\n'
        f'dhcpKea:\n  relayVIP: "{relay_vip}"\n'
    )
    return _helmchartconfig_upsert("spatiumddi-appliance", values)


def patch_node_labels(
    node_name: str,
    set_labels: dict[str, str | None],
) -> tuple[bool, str | None]:
    """Add or remove labels on a node via a JSON-merge-patch.

    ``set_labels`` keys map to label names; values map to label
    values (string) or ``None`` to remove the label. Single round
    trip — kubeapi applies the diff atomically.

    Idempotent: setting a label to its current value or removing a
    label that doesn't exist is a no-op server-side.

    Phase 10 (#183) entry point for the supervisor's role-apply
    path. The chart templates' per-role nodeSelector
    (``spatium.io/role-dns-bind9: "true"`` etc.) gates pod
    scheduling on the matching label being on the node. The
    supervisor calls this when a role joins/leaves the desired
    set.
    """
    if not set_labels:
        return True, None
    # JSON merge-patch on a Node resource: ``{"metadata":
    # {"labels": {"key": "value"}}}`` sets a label;
    # ``{"key": null}`` removes it.
    labels: dict[str, str | None] = dict(set_labels)
    payload = json.dumps({"metadata": {"labels": labels}}).encode("utf-8")
    path = f"/api/v1/nodes/{quote(node_name)}"
    try:
        status, resp = _request(
            "PATCH",
            path,
            body=payload,
            content_type="application/merge-patch+json",
        )
    except RuntimeError as exc:
        return False, str(exc)
    if status in (200, 201):
        return True, None
    return False, f"kubeapi status {status}: {resp[:200]!r}"


def patch_cnpg_instances(
    instances: int,
    *,
    pod_anti_affinity_type: str = "required",
    cluster_name: str = "spatium-control-spatiumddi-postgresql",
    namespace: str = "spatium",
) -> tuple[bool, str | None]:
    """Directly reconcile the CNPG ``Cluster`` CR's ``spec.instances`` and
    its instance-spreading policy.

    #272 — the CNPG Cluster carries ``helm.sh/resource-policy: keep`` so
    a failed-release recovery (uninstall+reinstall) can't delete it and
    wipe the database. But ``keep`` also makes the k3s helm-controller
    leave the resource's *spec* untouched on upgrade: when the seed
    scales the control plane via the spatium-control HelmChartConfig,
    Helm patches api/worker/frontend/redis to the new size but silently
    skips the kept Cluster, so CNPG stays at its initial instance count
    (observed live: a 1->3 promote left Postgres single-node while
    everything else scaled). Patch the Cluster CR directly here instead —
    a merge-patch isn't a Helm operation, so ``keep`` doesn't apply, and
    the CNPG operator reconciles the new replica set normally.

    #590 — ``spec.affinity.podAntiAffinityType`` rides the same patch, and
    for the same reason: the chart can set it on a FRESH install, but an
    appliance that A/B-upgrades into the fix would keep CNPG's ``preferred``
    default forever, since Helm won't touch the kept Cluster. Observed live
    on a 1→3 promote: instances 1 and 2 both landed on the seed, so one node
    loss would have taken the primary and a replica together.

    Note this can strand an instance whose PVC is already bound to a node
    that now hosts another instance — it goes Pending until the operator
    deletes that REPLICA's PVC (never the primary's) and lets CNPG re-clone
    it. Postgres stays available throughout: the primary is untouched and a
    surviving replica keeps failover possible. See charts/spatiumddi/
    README.md.

    Idempotent: GETs the current spec first and only PATCHes on a real
    change, so steady-state heartbeats stay quiet. Returns
    ``(changed, error)`` mirroring the other override helpers.
    """
    if instances < 1:
        return False, "instances < 1"
    if pod_anti_affinity_type not in ("preferred", "required"):
        return False, f"bad pod_anti_affinity_type {pod_anti_affinity_type!r}"
    base = (
        f"/apis/postgresql.cnpg.io/v1/namespaces/{quote(namespace)}"
        f"/clusters/{quote(cluster_name)}"
    )
    # Read current spec — skip the PATCH (and the heartbeat "applied" log)
    # when it already matches. A 404 means the Cluster isn't up yet (early
    # boot / not a cnpg deployment); treat as a quiet no-op, not an error.
    try:
        status, resp = _request("GET", base)
    except RuntimeError as exc:
        return False, str(exc)
    if status == 404:
        return False, None
    if status != 200:
        return False, f"kubeapi GET status {status}: {resp[:200]!r}"
    try:
        spec = json.loads(resp).get("spec", {})
        current = spec.get("instances")
        current_affinity = spec.get("affinity", {}) or {}
        current_aa = current_affinity.get("podAntiAffinityType")
        current_enabled = current_affinity.get("enablePodAntiAffinity")
    except (ValueError, AttributeError):
        current = current_aa = current_enabled = None
    if (
        current == instances
        and current_aa == pod_anti_affinity_type
        and current_enabled is True
    ):
        return False, None
    payload = json.dumps(
        {
            "spec": {
                "instances": instances,
                # merge-patch: this merges INTO spec.affinity, leaving the
                # chart's nodeSelector + tolerations under it untouched.
                "affinity": {
                    "enablePodAntiAffinity": True,
                    "podAntiAffinityType": pod_anti_affinity_type,
                    "topologyKey": "kubernetes.io/hostname",
                },
            }
        }
    ).encode("utf-8")
    try:
        status, resp = _request(
            "PATCH",
            base,
            body=payload,
            content_type="application/merge-patch+json",
        )
    except RuntimeError as exc:
        return False, str(exc)
    if status in (200, 201):
        return True, None
    return False, f"kubeapi PATCH status {status}: {resp[:200]!r}"


__all__ = [
    "KubeConfig",
    "PodStatus",
    "apply_metallb_overrides",
    "apply_control_plane_overrides",
    "apply_helmchart",
    "check_kubeapi_ready",
    "delete_helmchart",
    "delete_node",
    "get_config",
    "patch_cnpg_instances",
    "patch_node_labels",
    "list_pods",
]
