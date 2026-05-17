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
    # the CA bytes are inline (base64-encoded). For Phase 3's minimal
    # client we accept that introspection-via-host-kubeconfig is
    # best-effort + write a CA-bundle temp file once at process start.
    ca_path: str | None = None
    ca_b64 = cluster.get("certificate-authority-data")
    if ca_b64:
        import base64  # noqa: PLC0415

        tmp = Path("/tmp/.spatium-k3s-ca.crt")
        try:
            tmp.write_bytes(base64.b64decode(ca_b64))
            ca_path = str(tmp)
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
    """Build an SSLContext that verifies the kubeapi server cert."""
    ctx = ssl.create_default_context()
    if ca_path:
        ctx.load_verify_locations(cafile=ca_path)
    else:
        # No CA path — in-cluster path always has one, so this is the
        # host-kubeconfig fallback without a CA. Permit but warn.
        log.warning("supervisor.k8s_api.ssl_unverified")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


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


__all__ = [
    "KubeConfig",
    "PodStatus",
    "apply_helmchart",
    "check_kubeapi_ready",
    "delete_helmchart",
    "get_config",
    "patch_node_labels",
    "list_pods",
]
