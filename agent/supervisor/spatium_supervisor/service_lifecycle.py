"""Supervisor service-lifecycle module (#183 Phase 7 — k3s-only).

Owns the supervisor's orchestration plane: applies role assignments
from the control-plane heartbeat to the local k3s by PATCHing
``HelmChart`` Custom Resources into the kubeapi. k3s's bundled
helm-controller picks the CRs up + runs ``helm upgrade --install``
for us on the next reconcile cycle.

Before Phase 7 there was a parallel docker-compose path in this
module + ``docker_api.py``. Phase 7 retires docker entirely; both
are deleted, and the k3s path graduates to the only path.

Design notes:

* The supervisor doesn't run ``helm`` itself. We construct a
  ``HelmChart`` CR carrying ``spec.chartContent`` (base64-encoded
  tarball) + ``spec.valuesContent`` (rendered YAML) and PATCH it.

* The chart tarball is **baked into the slot** at
  ``/usr/lib/spatiumddi/charts/spatiumddi-appliance.tgz`` by the
  build-time ``appliance/scripts/bake-chart.sh`` script. Air-gap
  friendly: no chart registry, no internet calls, no ``helm pull``
  at runtime.

* Values are derived from the heartbeat-response ``role_assignment``
  shape (rendered by ``role_orchestrator``).
  ``COMPOSE_PROFILES`` keys translate to per-role
  ``<role>.enabled: true`` flags + the agent keys / group names /
  control-plane URL.

Failures are surfaced as ``state="failed"`` with a single-line
``reason`` so the Fleet drilldown's red banner stays readable.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

from . import k8s_api


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of one ``apply_role_assignment`` or
    ``tear_down_supervised_services`` pass.

    ``state`` mirrors what the supervisor reports in the next
    heartbeat under ``role_switch_state``: ``idle`` / ``ready`` /
    ``failed``. ``reason`` carries the failure detail (kubeapi
    error first line is usually enough) so the operator can triage
    without SSH-ing in.
    """

    state: str  # ready | failed | idle
    reason: str | None = None
    started: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()


# Service names the appliance can run. Match the chart's component
# names (``app.kubernetes.io/component`` labels). The watchdog uses
# this set to enumerate "which pods should I expect".
SUPERVISED_SERVICES: tuple[str, ...] = ("dns-bind9", "dns-powerdns", "dhcp-kea")

log = structlog.get_logger(__name__)

# Baked-chart path the build-time script writes (#183 Phase 3).
# Sibling to /usr/lib/spatiumddi/images/*.tar.zst — same lifecycle
# (slot-baked at build, mounted via mkosi.extra/ copy).
_BAKED_CHART_TARBALL = Path("/usr/lib/spatiumddi/charts/spatiumddi-appliance.tgz")

# HelmChart CR name + namespaces. Single chart per appliance — one
# install drives every assigned role via per-role enabled flags.
# The CR itself lives in kube-system (where helm-controller watches);
# the deployed pods live in the dedicated "spatium" namespace.
_HELMCHART_NAME = "spatiumddi-appliance"
_CHART_NAMESPACE = "kube-system"
_TARGET_NAMESPACE = "spatium"

# Profile → Helm chart key mapping. ``compose_profiles`` from the
# rendered env file uses compose-style names; the chart's values.yaml
# uses camelCase per-role blocks.
_PROFILE_TO_HELM_KEY = {
    "dns-bind9": "dnsBind9",
    "dns-powerdns": "dnsPowerdns",
    "dhcp": "dhcpKea",
}

# Phase 10 (#183) — every role the chart templates have a per-role
# nodeSelector for. The supervisor labels the node with
# ``spatium.io/role-<key>=true`` for each role in the desired set
# and clears the label for each role leaving. Pod scheduling gates
# on the label being present, so role swap = label flip.
#
# Keep in lock-step with the per-template ``nodeSelector`` blocks in
# charts/spatiumddi-appliance/templates/*.yaml.
_ROLE_LABEL_KEYS = {
    "dns-bind9": "spatium.io/role-dns-bind9",
    "dns-powerdns": "spatium.io/role-dns-powerdns",
    "dhcp": "spatium.io/role-dhcp",
}

# Env keys to lift from the rendered role-compose env file into chart
# values. The compose path interpolates them at ``docker compose up``;
# the k3s path threads them into the chart's values.yaml structure.
_ENV_PASSTHROUGH_KEYS = {
    "AGENT_GROUP",
    "DNS_ENGINE",
    "DNS_AGENT_KEY",
    "DHCP_AGENT_GROUP",
    "DHCP_NETWORK_MODE",
    "DHCP_AGENT_KEY",
    "CONTROL_PLANE_URL",
    "SPATIUMDDI_VERSION",
}


@dataclass(frozen=True)
class K3sEnvironment:
    """Result of probing whether k3s is the live runtime."""

    available: bool
    reason: str | None = None


def k3s_available() -> K3sEnvironment:
    """Return whether the k3s path is ready to use.

    Checks (all must pass):
      * Chart tarball baked into the slot
      * Kubeapi reachable (``/readyz`` returns ok)

    Returns an ``unavailable`` with a human reason when any fails
    so heartbeat-level logging can show *why* the supervisor stayed
    on docker compose this tick."""
    if not _BAKED_CHART_TARBALL.exists():
        return K3sEnvironment(available=False, reason="chart tarball not baked")
    if not k8s_api.check_kubeapi_ready():
        return K3sEnvironment(available=False, reason="kubeapi /readyz not ok")
    return K3sEnvironment(available=True)


def _parse_env_file(env_file: Path) -> dict[str, str]:
    """Read the rendered role-compose env file into a dict. Same
    format render_env_file produces: ``KEY=value`` lines, comments
    prefixed with ``#``, blanks ignored."""
    out: dict[str, str] = {}
    if not env_file.exists():
        return out
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("supervisor.k3s_lifecycle.env_read_failed", error=str(exc))
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _build_values(profiles: list[str], env_vars: dict[str, str]) -> dict[str, object]:
    """Construct the Helm values dict from the active profile set +
    rendered env. Mirrors the per-role values.yaml structure in
    ``charts/spatiumddi-appliance/``.

    Air-gap defaults are inherited from the chart's values.yaml;
    here we only override what changes per-appliance (per-role
    enabled flags + agent keys + group names + control-plane URL).
    """
    control_plane_url = env_vars.get("CONTROL_PLANE_URL") or os.environ.get(
        "CONTROL_PLANE_URL", ""
    )
    image_tag = env_vars.get("SPATIUMDDI_VERSION") or os.environ.get(
        "SPATIUMDDI_VERSION", "dev"
    )

    # Phase 10 wave 2 — ``enabled`` flags here are RELEASE-ownership
    # scope (which helm release owns which Deployment), NOT
    # role-scheduling scope. Scheduling is gated by node labels
    # exclusively (``spatium.io/role-<role>=true``); the supervisor
    # toggles those on every heartbeat via reconcile_node_labels.
    #
    # The role release ALWAYS sets every role's enabled=true (so the
    # chart renders + helm tracks the Deployment + PVCs). The
    # bootstrap release sets them all false (so spatium-bootstrap
    # doesn't fight us for ownership). Result: role swap = pure
    # label flip, no chart upgrade, no chartContent re-upload to
    # kine — Phase 9 kine-footprint follow-up closes.
    values: dict[str, object] = {
        "global": {
            "imageTag": image_tag,
            "imagePullPolicy": "Never",
        },
        "agentLanding": {
            "enabled": False,
        },
        "supervisor": {
            "enabled": False,
        },
        "dnsBind9": {
            "enabled": True,
            "controlPlaneUrl": control_plane_url,
            "agentKey": env_vars.get("DNS_AGENT_KEY", ""),
            "serverGroupName": env_vars.get("AGENT_GROUP", ""),
        },
        "dnsPowerdns": {
            "enabled": True,
            "controlPlaneUrl": control_plane_url,
            "agentKey": env_vars.get("DNS_AGENT_KEY", ""),
            "serverGroupName": env_vars.get("AGENT_GROUP", ""),
        },
        "dhcpKea": {
            "enabled": True,
            "controlPlaneUrl": control_plane_url,
            "agentKey": env_vars.get("DHCP_AGENT_KEY", ""),
            "serverGroupName": env_vars.get("DHCP_AGENT_GROUP", "")
            or env_vars.get("AGENT_GROUP", ""),
            "networkMode": env_vars.get("DHCP_NETWORK_MODE", "host"),
        },
    }
    return values


def _read_chart_tarball() -> bytes:
    """Load the baked chart tarball off the slot rootfs. Raises
    ``FileNotFoundError`` if the bake didn't run — caller surfaces
    this as a ``failed`` LifecycleResult."""
    return _BAKED_CHART_TARBALL.read_bytes()


def apply_role_assignment(
    profiles: list[str],
    env_file: Path,
) -> LifecycleResult:
    """k3s analog of ``service_lifecycle.apply_role_assignment``.

    Reads the rendered env file for control-plane URL + per-role
    agent keys, builds the chart values block, base64-encodes the
    baked chart tarball, and PATCHes a HelmChart CR into the
    appliance's local kubeapi. k3s's helm-controller reconciles
    the CR into a Helm release on its next loop (typically <5s).

    Returns ``ready`` on PATCH success, ``idle`` when k3s isn't
    available (no chart baked / kubeapi unreachable — caller's
    fallback to the compose path), ``failed`` on a kubeapi or
    serialisation error.
    """
    env = k3s_available()
    if not env.available:
        # ``idle`` instead of ``failed`` mirrors the compose path's
        # "compose not available" shape: the supervisor isn't broken,
        # this just isn't the runtime here. Caller (heartbeat) reads
        # ``state="idle"`` as "skip + report we did nothing".
        return LifecycleResult(state="idle", reason=env.reason)

    env_vars = _parse_env_file(env_file)
    values = _build_values(profiles, env_vars)

    try:
        chart_bytes = _read_chart_tarball()
    except OSError as exc:
        return LifecycleResult(state="failed", reason=f"chart read: {exc}")
    chart_b64 = base64.b64encode(chart_bytes).decode("ascii")

    ok, err = k8s_api.apply_helmchart(
        _HELMCHART_NAME,
        chart_content_b64=chart_b64,
        values=values,
        target_namespace=_TARGET_NAMESPACE,
        chart_namespace=_CHART_NAMESPACE,
    )
    if not ok:
        # Compose stderr first-line is usually enough for the Fleet
        # UI banner; kubeapi errors are similarly short.
        return LifecycleResult(state="failed", reason=err or "kubeapi apply failed")

    # Phase 10 (#183) — alongside the values PATCH, label the node
    # with ``spatium.io/role-<role>=true`` for each desired role,
    # and clear the label for each role not in the desired set.
    # The chart's per-role nodeSelector gates scheduling on this,
    # so a role swap (BIND9 → PowerDNS) becomes "label flip" instead
    # of a chart upgrade race. Best-effort: a label-patch failure
    # doesn't block the apply (the values PATCH already landed and
    # the helm-install will sit Pending until the next reconcile
    # writes the labels).
    desired_role_set = {p for p in profiles if p in _ROLE_LABEL_KEYS}
    label_diff: dict[str, str | None] = {}
    for role, label in _ROLE_LABEL_KEYS.items():
        label_diff[label] = "true" if role in desired_role_set else None
    node_name = os.environ.get("NODE_NAME") or os.environ.get("APPLIANCE_HOSTNAME") or ""
    if not node_name:
        try:
            import socket as _socket
            node_name = _socket.gethostname()
        except OSError:
            node_name = ""
    if node_name:
        label_ok, label_err = k8s_api.patch_node_labels(node_name, label_diff)
        if not label_ok:
            log.warning(
                "supervisor.k3s_lifecycle.label_patch_failed",
                node=node_name,
                error=label_err,
            )
        else:
            log.info(
                "supervisor.k3s_lifecycle.labels_applied",
                node=node_name,
                set=[k for k, v in label_diff.items() if v is not None],
                cleared=[k for k, v in label_diff.items() if v is None],
            )

    desired_services = tuple(sorted(p for p in profiles if p in _PROFILE_TO_HELM_KEY))
    log.info(
        "supervisor.k3s_lifecycle.applied",
        profiles=list(profiles),
        services=list(desired_services),
        control_plane_url=(
            values["dnsBind9"]["controlPlaneUrl"]  # type: ignore[index]
            if isinstance(values.get("dnsBind9"), dict)
            else None
        ),
    )
    # ``started`` reports the FULL desired set; reconciliation
    # idempotency means re-applying with the same set is cheap
    # (helm-controller short-circuits when the release hash hasn't
    # moved). Watchdog confirms each pod is healthy independently.
    return LifecycleResult(state="ready", started=desired_services)


def reconcile_node_labels(profiles: list[str]) -> tuple[bool, str | None]:
    """Reconcile the node's per-role labels against ``profiles``.

    Idempotent — setting a label to its current value is a kubeapi
    no-op. Cheap enough (single PATCH, <10 ms locally) to run on
    every heartbeat tick alongside the apply_role_assignment
    skip-check. Catches drift from an out-of-band ``kubectl label``
    or a manual unlabeling without waiting for the values-hash to
    change.

    Returns ``(ok, error_or_None)`` — caller logs but doesn't act
    on failure (next heartbeat re-attempts).
    """
    desired_role_set = {p for p in profiles if p in _ROLE_LABEL_KEYS}
    label_diff: dict[str, str | None] = {}
    for role, label in _ROLE_LABEL_KEYS.items():
        label_diff[label] = "true" if role in desired_role_set else None
    node_name = (
        os.environ.get("NODE_NAME") or os.environ.get("APPLIANCE_HOSTNAME") or ""
    )
    if not node_name:
        try:
            import socket as _socket
            node_name = _socket.gethostname()
        except OSError:
            return False, "node_name unknown"
    return k8s_api.patch_node_labels(node_name, label_diff)


def tear_down_supervised_services() -> LifecycleResult:
    """k3s analog of ``service_lifecycle.tear_down_supervised_services``.

    Deletes the HelmChart CR. helm-controller catches the delete +
    runs ``helm uninstall`` against the spatium namespace. Idempotent
    — deleting a non-existent CR is a no-op.

    Called by heartbeat on revocation (control plane removed our
    approval) so the appliance stops running its assigned services.
    """
    env = k3s_available()
    if not env.available:
        return LifecycleResult(state="idle", reason=env.reason)

    ok, err = k8s_api.delete_helmchart(
        _HELMCHART_NAME, chart_namespace=_CHART_NAMESPACE
    )
    if not ok:
        return LifecycleResult(state="failed", reason=err or "kubeapi delete failed")

    # Phase 10 (#183) — clear every per-role label so pods that
    # somehow survived the chart delete don't keep running on the
    # node. helm-controller's uninstall should remove the
    # Deployments first, but this is belt-and-braces.
    node_name = os.environ.get("NODE_NAME") or os.environ.get("APPLIANCE_HOSTNAME") or ""
    if not node_name:
        try:
            import socket as _socket
            node_name = _socket.gethostname()
        except OSError:
            node_name = ""
    if node_name:
        label_diff: dict[str, str | None] = {label: None for label in _ROLE_LABEL_KEYS.values()}
        k8s_api.patch_node_labels(node_name, label_diff)
    log.warning("supervisor.k3s_lifecycle.torn_down")
    # Report every supervised service as ``stopped`` — we deleted
    # the chart that owned every one of them. The Fleet drilldown's
    # role-switch banner reads the same shape regardless of runtime.
    return LifecycleResult(state="ready", stopped=tuple(SUPERVISED_SERVICES))


__all__ = [
    "apply_role_assignment",
    "k3s_available",
    "reconcile_node_labels",
    "tear_down_supervised_services",
]
