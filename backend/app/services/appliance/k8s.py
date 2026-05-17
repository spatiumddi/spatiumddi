"""Minimal kubeapi client for the api pod.

The api pod has a ServiceAccount mounted at
``/var/run/secrets/kubernetes.io/serviceaccount/`` (token + ca.crt
+ namespace) when deployed via the umbrella chart with
``api.serviceAccount.enabled=true``. We use stdlib http.client +
ssl rather than the upstream ``kubernetes`` Python SDK to avoid
the ~30 MB extra wheel + transitive deps for a handful of
endpoints.

Pattern is identical to ``agent/supervisor/spatium_supervisor/
k8s_api.py`` — same in-cluster ServiceAccount auth, same single-
connection-per-call shape.

Used by:

* ``services/appliance/containers.py`` — list pods + tail logs
  for the Fleet UI's Pods tab (post-Phase-11 rename of the old
  /appliance/containers docker surface).
* ``services/appliance/deployment.py`` — patch the
  ``spatium-appliance-tls`` Secret in place when an operator
  uploads a cert through the Cert Manager UI. Replaces the
  pre-Phase-11 ``/var/run/docker.sock`` SIGHUP-the-frontend
  shape.

Gated on ``settings.appliance_mode`` at every call site so non-
appliance deploys never look for the ServiceAccount.
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, overload
from urllib.parse import quote

import structlog

logger = structlog.get_logger(__name__)


_SA_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_SA_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
_SA_NAMESPACE_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


class KubeapiUnavailableError(RuntimeError):
    """Raised when the kubeapi can't be reached.

    Router translates to 503 Service Unavailable. Distinct from
    a NotFound / Forbidden from kubeapi itself (those come back
    via the (status, body) return shape on each helper).
    """


@dataclass(frozen=True)
class _Config:
    host: str
    port: int
    token: str
    ca_path: str
    namespace: str


_CACHED_CONFIG: _Config | None = None
_CONFIG_LOADED_AT: float = 0.0
# Re-read the ServiceAccount token every 10 min so a rotated
# projected token doesn't strand us on a stale value. kubelet
# projects with 1h TTL by default; 10 min is well under that.
_CONFIG_TTL_S = 600.0


def _resolve_config() -> _Config | None:
    """Read SA token / ca / namespace from the projected volume.

    Returns None when the volume isn't mounted (non-appliance
    deploy, or chart installed without the api.serviceAccount.
    enabled flag). Caller treats None as "kubeapi not configured;
    fall back to whatever the legacy code path did".
    """
    if not (_SA_TOKEN_PATH.exists() and _SA_CA_PATH.exists()):
        return None
    try:
        token = _SA_TOKEN_PATH.read_text(encoding="utf-8").strip()
        ns = (
            _SA_NAMESPACE_PATH.read_text(encoding="utf-8").strip()
            if _SA_NAMESPACE_PATH.exists()
            else "default"
        )
    except OSError as exc:
        logger.warning("k8s_sa_read_failed", error=str(exc))
        return None
    # In-cluster kubeapi is reachable on ``kubernetes.default.svc``.
    # The env vars ``KUBERNETES_SERVICE_HOST`` / ``_PORT_HTTPS``
    # are set by kubelet for every pod; prefer them over hardcoded
    # values so a non-default service port still works.
    import os  # noqa: PLC0415

    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = int(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443"))
    return _Config(host=host, port=port, token=token, ca_path=str(_SA_CA_PATH), namespace=ns)


def get_config() -> _Config | None:
    """Cached config accessor — refreshed every 10 min."""
    global _CACHED_CONFIG, _CONFIG_LOADED_AT
    now = time.monotonic()
    if _CACHED_CONFIG is None or now - _CONFIG_LOADED_AT > _CONFIG_TTL_S:
        _CACHED_CONFIG = _resolve_config()
        _CONFIG_LOADED_AT = now
    return _CACHED_CONFIG


def _ssl_context(ca_path: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=ca_path)
    return ctx


@overload
def _request(
    method: str,
    path: str,
    *,
    body: bytes | None = ...,
    content_type: str | None = ...,
    timeout: float = ...,
    stream: Literal[False] = False,
) -> tuple[int, bytes]:
    pass


@overload
def _request(
    method: str,
    path: str,
    *,
    body: bytes | None = ...,
    content_type: str | None = ...,
    timeout: float = ...,
    stream: Literal[True],
) -> tuple[int, http.client.HTTPResponse]:
    pass


def _request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 10.0,
    stream: bool = False,
) -> tuple[int, bytes | http.client.HTTPResponse]:
    """Issue a request to kubeapi.

    When ``stream=True`` returns the live ``HTTPResponse`` so
    callers can iterate over chunks (pod log follow). The caller
    MUST close the connection — use the streaming SSE wrapper for
    that.
    """
    cfg = get_config()
    if cfg is None:
        raise KubeapiUnavailableError("ServiceAccount not mounted; kubeapi unreachable")
    conn = http.client.HTTPSConnection(
        cfg.host, cfg.port, timeout=timeout, context=_ssl_context(cfg.ca_path)
    )
    try:
        headers = {
            "Host": cfg.host,
            "Authorization": f"Bearer {cfg.token}",
            "Accept": "application/json",
        }
        if content_type:
            headers["Content-Type"] = content_type
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        if stream:
            # Caller owns the conn — return both so they can close.
            return resp.status, resp
        return resp.status, resp.read()
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise KubeapiUnavailableError(f"kubeapi {method} {path}: {exc}") from exc
    finally:
        if not stream:
            conn.close()


# ── Endpoint helpers ──────────────────────────────────────────────────


def list_pods(namespace: str | None = None) -> list[dict[str, Any]]:
    """List pods in ``namespace`` (defaults to the api pod's own
    namespace from the ServiceAccount projection).

    Returns the raw kubeapi PodList ``items`` array. Caller is
    responsible for flattening to whatever summary shape it
    needs.
    """
    cfg = get_config()
    if cfg is None:
        raise KubeapiUnavailableError("ServiceAccount not mounted; kubeapi unreachable")
    ns = namespace or cfg.namespace
    path = f"/api/v1/namespaces/{quote(ns)}/pods"
    status, body = _request("GET", path)
    if status != 200:
        raise KubeapiUnavailableError(f"kubeapi list pods status {status}: {body[:200]!r}")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise KubeapiUnavailableError(f"kubeapi list pods bad json: {exc}") from exc
    return data.get("items") or []


def get_pod_logs(
    name: str,
    namespace: str | None = None,
    *,
    tail: int = 200,
    since_seconds: int | None = None,
    container: str | None = None,
) -> str:
    """Fetch tail of pod logs.

    Uses ``GET /api/v1/namespaces/<ns>/pods/<name>/log``. Single-
    shot read; for SSE-streaming follow use ``stream_pod_logs``.
    """
    cfg = get_config()
    if cfg is None:
        raise KubeapiUnavailableError("ServiceAccount not mounted; kubeapi unreachable")
    ns = namespace or cfg.namespace
    qs = [f"tailLines={tail}"]
    if since_seconds is not None:
        qs.append(f"sinceSeconds={since_seconds}")
    if container:
        qs.append(f"container={quote(container)}")
    path = f"/api/v1/namespaces/{quote(ns)}/pods/{quote(name)}/log?{'&'.join(qs)}"
    status, body = _request("GET", path)
    if status == 404:
        raise KubeapiUnavailableError(f"pod {ns}/{name} not found")
    if status != 200:
        raise KubeapiUnavailableError(f"kubeapi pod logs status {status}: {body[:200]!r}")
    return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)


def stream_pod_logs(
    name: str,
    namespace: str | None = None,
    *,
    tail: int = 200,
    container: str | None = None,
):
    """Yield log lines (str) as kubeapi emits them. ``follow=true``
    so the call blocks while the pod runs + closes on pod exit.

    Generator caller iterates; the underlying conn is closed when
    the generator is garbage-collected or .close() is invoked.
    Use in SSE handlers like:

        async def events():
            for line in stream_pod_logs(...):
                yield f"data: {line}\\n\\n"

    NOTE: this is a synchronous generator. The router wraps it in
    ``anyio.to_thread.run_sync`` for the async-FastAPI handler so
    we don't block the event loop.
    """
    cfg = get_config()
    if cfg is None:
        raise KubeapiUnavailableError("ServiceAccount not mounted; kubeapi unreachable")
    ns = namespace or cfg.namespace
    qs = [f"tailLines={tail}", "follow=true"]
    if container:
        qs.append(f"container={quote(container)}")
    path = f"/api/v1/namespaces/{quote(ns)}/pods/{quote(name)}/log?{'&'.join(qs)}"
    conn = http.client.HTTPSConnection(
        cfg.host, cfg.port, timeout=None, context=_ssl_context(cfg.ca_path)
    )
    try:
        # ``Accept: text/plain`` fails kubeapi's media-type check with
        # 406 — the api server only allows application/json,
        # application/yaml, application/vnd.kubernetes.protobuf,
        # application/cbor, or ``*/*``. Pod logs are returned as raw
        # text regardless. Use ``*/*`` so we don't accidentally
        # constrain the negotiation.
        headers = {
            "Host": cfg.host,
            "Authorization": f"Bearer {cfg.token}",
            "Accept": "*/*",
        }
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            err_body = resp.read()[:200]
            raise KubeapiUnavailableError(
                f"kubeapi pod logs stream status {resp.status}: {err_body!r}"
            )
        # Stream line-by-line. fp.readline() blocks on chunked
        # transfer; closing the conn breaks the read with an
        # exception — caller should swallow + re-raise as needed.
        while True:
            line = resp.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").rstrip("\n")
    finally:
        conn.close()


def delete_pod(name: str, namespace: str | None = None) -> tuple[bool, str | None]:
    """Delete a pod. The owning Deployment/DaemonSet recreates it
    — this is the k8s analog of ``docker restart``.
    """
    cfg = get_config()
    if cfg is None:
        return False, "ServiceAccount not mounted"
    ns = namespace or cfg.namespace
    path = f"/api/v1/namespaces/{quote(ns)}/pods/{quote(name)}"
    try:
        status, body = _request("DELETE", path)
    except KubeapiUnavailableError as exc:
        return False, str(exc)
    if status in (200, 202, 404):
        return True, None
    return False, f"kubeapi status {status}: {body[:200]!r}"


def patch_secret(
    name: str, data: dict[str, str], namespace: str | None = None
) -> tuple[bool, str | None]:
    """Strategic-merge-patch a Secret's ``data`` block in place.

    ``data`` keys are filenames inside the Secret (e.g. ``tls.crt``);
    values are the PEM contents — this function does the base64
    encoding the kubeapi expects on the wire.

    Used by the cert-deploy path to update
    ``spatium-appliance-tls`` after an operator-uploaded cert
    activates.
    """
    import base64  # noqa: PLC0415

    cfg = get_config()
    if cfg is None:
        return False, "ServiceAccount not mounted"
    ns = namespace or cfg.namespace
    encoded = {
        k: base64.b64encode(v.encode("utf-8") if isinstance(v, str) else v).decode("ascii")
        for k, v in data.items()
    }
    payload = json.dumps({"data": encoded}).encode("utf-8")
    path = f"/api/v1/namespaces/{quote(ns)}/secrets/{quote(name)}"
    try:
        status, body = _request(
            "PATCH",
            path,
            body=payload,
            content_type="application/strategic-merge-patch+json",
        )
    except KubeapiUnavailableError as exc:
        return False, str(exc)
    if status in (200, 201):
        return True, None
    return False, f"kubeapi status {status}: {body[:200]!r}"


def patch_deployment_annotation(
    name: str,
    key: str,
    value: str,
    namespace: str | None = None,
) -> tuple[bool, str | None]:
    """Bump a single annotation on a Deployment's pod template.

    Used to trigger a rollout after a referenced Secret has been
    patched (k8s doesn't auto-roll on Secret changes — the pod
    template's annotation acts as a rollout trigger).
    """
    cfg = get_config()
    if cfg is None:
        return False, "ServiceAccount not mounted"
    ns = namespace or cfg.namespace
    payload = json.dumps(
        {"spec": {"template": {"metadata": {"annotations": {key: value}}}}}
    ).encode("utf-8")
    path = f"/apis/apps/v1/namespaces/{quote(ns)}/deployments/{quote(name)}"
    try:
        status, body = _request(
            "PATCH",
            path,
            body=payload,
            content_type="application/strategic-merge-patch+json",
        )
    except KubeapiUnavailableError as exc:
        return False, str(exc)
    if status in (200, 201):
        return True, None
    return False, f"kubeapi status {status}: {body[:200]!r}"


__all__ = [
    "KubeapiUnavailableError",
    "delete_pod",
    "get_pod_logs",
    "list_pods",
    "patch_deployment_annotation",
    "patch_secret",
    "stream_pod_logs",
]
