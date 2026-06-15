"""Provider-neutral contracts for the Cloud infrastructure mirror (#37 A).

A **connector** (``services/cloud/{aws,azure,gcp}.py``) translates one
provider's SDK responses into a single normalised :class:`CloudInventory`.
The shared **reconciler** (``services/cloud/reconcile.py``) upserts that
inventory into IPAM (one ``IPBlock`` per VPC CIDR, ``Subnet`` rows
beneath, ``IPAddress`` rows for instance NICs / public IPs / LB
frontends). Per CLAUDE.md non-negotiable #10 the service layer speaks
only to this interface and resolves a concrete connector lazily via
:func:`get_connector` — so adding a provider never edits this file.

Credential dict shapes (decrypted from ``CloudEndpoint.credentials_encrypted``):
    aws   → {"access_key_id": str, "secret_access_key": str}
    azure → {"tenant_id": str, "client_id": str, "client_secret": str}
    gcp   → {"service_account_json": str}   # the whole key file as a string

Non-secret routing (``CloudEndpoint.provider_config``):
    azure → {"subscription_ids": [str, ...]}
    gcp   → {"project_ids": [str, ...]}
    aws   → {}                              # AWS scopes by ``regions``

Connectors must be safe to construct per call, perform no I/O in
``__init__``, and wrap blocking SDK calls in ``asyncio.to_thread`` (the
public methods are ``async``). SDK imports are done lazily inside methods
so importing the connector module never hard-fails on a missing optional
dependency and tests can patch the client factory.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class CloudConnectorError(Exception):
    """Raised by connectors on auth / API / unsupported-provider failures.

    The reconciler + test-connection probe catch this and surface
    ``str(exc)`` to the operator as ``last_sync_error`` / a probe message.
    """


# ── Normalised inventory dataclasses ───────────────────────────────────
#
# These are the *only* shapes the reconciler consumes. Provider-specific
# quirks are flattened away in the connector. ``region`` is a free string
# (AWS region / Azure location / GCP region) used purely for naming +
# the discovery snapshot; ``extra`` carries provider tags/labels that the
# reconciler may pass through to ``custom_fields`` (Phase 4+).


@dataclass(frozen=True)
class CloudNetwork:
    """A VPC (AWS) / VNet (Azure) / VPC network (GCP)."""

    id: str
    name: str
    cidrs: tuple[str, ...]  # address-space CIDR(s); one IPBlock per entry
    region: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudSubnet:
    """A subnet within a :class:`CloudNetwork`."""

    id: str
    name: str
    network_id: str
    cidr: str
    region: str | None = None
    # First usable host by cloud convention (x.x.x.1) unless the provider
    # reports an explicit gateway. ``None`` lets the reconciler derive it.
    gateway: str | None = None


@dataclass(frozen=True)
class CloudNic:
    """One network interface on a :class:`CloudInstance`."""

    private_ip: str
    public_ip: str | None = None
    mac: str | None = None


@dataclass(frozen=True)
class CloudInstance:
    """A VM (Azure) / EC2 instance (AWS) / GCE instance (GCP)."""

    id: str
    name: str
    running: bool
    nics: tuple[CloudNic, ...] = ()
    region: str | None = None


@dataclass(frozen=True)
class CloudPublicIP:
    """A public / Elastic / external IP address."""

    address: str
    name: str = ""
    attached: bool = False


@dataclass(frozen=True)
class CloudLoadBalancer:
    """A load balancer's frontend IP(s) (ELB/ALB/NLB, Azure LB, GCP rule)."""

    id: str
    name: str
    frontend_ips: tuple[str, ...] = ()
    region: str | None = None


@dataclass
class CloudInventory:
    """Everything one reconcile pass needs from a provider."""

    account_id: str
    networks: list[CloudNetwork] = field(default_factory=list)
    subnets: list[CloudSubnet] = field(default_factory=list)
    instances: list[CloudInstance] = field(default_factory=list)
    public_ips: list[CloudPublicIP] = field(default_factory=list)
    load_balancers: list[CloudLoadBalancer] = field(default_factory=list)
    # Non-fatal connector notes (a region that failed, an unsupported
    # resource skipped) surfaced into the reconcile summary warnings.
    warnings: list[str] = field(default_factory=list)
    # #430 — scopes (project / region / subscription) whose fetch failed
    # mid-pull. When non-empty the inventory is INCOMPLETE: the reconciler
    # still upserts what it got but MUST skip the absence-delete pass, or a
    # partial read would mass-delete rows for the missing scope.
    failed_scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CloudProbeResult:
    """Result of a test-connection probe."""

    ok: bool
    message: str
    account_id: str | None = None
    network_count: int | None = None
    instance_count: int | None = None


# ── Connector ABC ──────────────────────────────────────────────────────


class CloudConnector(ABC):
    """Abstract per-provider connector. Construct via :func:`get_connector`."""

    provider: str = "abstract"

    def __init__(
        self,
        *,
        credentials: dict[str, str],
        provider_config: dict,
        regions: list[str] | None = None,
    ) -> None:
        self.credentials = credentials or {}
        self.provider_config = provider_config or {}
        self.regions = list(regions or [])

    @abstractmethod
    async def probe(self) -> CloudProbeResult:
        """Cheap auth + reachability check for the test-connection button.

        Never raises for an expected failure (bad creds, network) — it
        returns ``CloudProbeResult(ok=False, message=...)``. Only truly
        unexpected programmer errors propagate.
        """

    @abstractmethod
    async def fetch_inventory(
        self,
        *,
        include_stopped: bool = False,
        include_load_balancers: bool = True,
    ) -> CloudInventory:
        """Pull the full normalised inventory.

        Raises :class:`CloudConnectorError` on auth / API failure so the
        reconciler can record ``last_sync_error`` and stop cleanly.
        """


# Provider → (module, class). Lazy so this file never changes when a
# connector module lands; the connector class names are part of the
# contract (the connector author must use exactly these).
_CONNECTOR_REGISTRY: dict[str, tuple[str, str]] = {
    "aws": ("app.services.cloud.aws", "AWSConnector"),
    "azure": ("app.services.cloud.azure", "AzureConnector"),
    "gcp": ("app.services.cloud.gcp", "GCPConnector"),
}


def implemented_providers() -> frozenset[str]:
    """Providers with a wired connector (gates the API ``provider`` field)."""
    return frozenset(_CONNECTOR_REGISTRY)


def get_connector(
    provider: str,
    *,
    credentials: dict[str, str],
    provider_config: dict,
    regions: list[str] | None = None,
) -> CloudConnector:
    """Instantiate the connector for ``provider``.

    Raises :class:`CloudConnectorError` for an unknown / unimplemented
    provider so the caller surfaces a clean message rather than a
    KeyError / ImportError.
    """
    spec = _CONNECTOR_REGISTRY.get(provider)
    if spec is None:
        raise CloudConnectorError(
            f"Cloud provider {provider!r} is not implemented "
            f"(supported: {', '.join(sorted(_CONNECTOR_REGISTRY))})."
        )
    module_name, class_name = spec
    try:
        module = importlib.import_module(module_name)
        connector_cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:  # pragma: no cover - wiring guard
        raise CloudConnectorError(
            f"Cloud connector for {provider!r} failed to load: {exc}"
        ) from exc
    return connector_cls(
        credentials=credentials,
        provider_config=provider_config,
        regions=regions,
    )


__all__ = [
    "CloudConnector",
    "CloudConnectorError",
    "CloudInstance",
    "CloudInventory",
    "CloudLoadBalancer",
    "CloudNetwork",
    "CloudNic",
    "CloudProbeResult",
    "CloudPublicIP",
    "CloudSubnet",
    "get_connector",
    "implemented_providers",
]
