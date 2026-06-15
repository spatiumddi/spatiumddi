"""GCP connector — translate Compute Engine resources into a CloudInventory.

GCP's data model is the odd one of the three providers (#37): a **VPC
network is global and carries no CIDR of its own** — the address ranges
live on (regional) *subnetworks*. So every :class:`CloudNetwork` we emit
has ``cidrs=()`` and the real CIDRs ride on the :class:`CloudSubnet` rows
beneath it. The reconciler knows to derive IPAM blocks from subnet CIDRs
when a network reports no address space.

Credential shape (decrypted from ``CloudEndpoint.credentials_encrypted``):
    {"service_account_json": "<the whole key file as a JSON string>"}

Routing (``CloudEndpoint.provider_config``):
    {"project_ids": [str, ...]}        # one inventory pass spans every id

``regions`` is an optional allow-list (empty = every region). It filters
regional subnetworks + the region we trim instance zones / addresses /
forwarding-rules down to; global resources are always included.

The python-compute client surfaces protobuf fields in a snake_case form
that splits on every internal capital, so ``networkIP`` → ``network_i_p``,
``natIP`` → ``nat_i_p``, ``IPAddress`` → ``i_p_address``. Those spellings
are load-bearing below — they are not typos.

Per CLAUDE.md the google SDK is imported lazily inside methods so this
module imports cleanly without ``google-cloud-compute`` installed and the
client factories can be monkeypatched in tests.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import structlog

from app.services.cloud.base import (
    CloudConnector,
    CloudConnectorError,
    CloudInstance,
    CloudInventory,
    CloudLoadBalancer,
    CloudNetwork,
    CloudNic,
    CloudProbeResult,
    CloudPublicIP,
    CloudSubnet,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from google.oauth2.service_account import Credentials

logger = structlog.get_logger(__name__)


def _basename(url: str | None) -> str:
    """Last path segment of a GCP self-link (or bare name).

    GCP cross-references resources by full self-link
    (``.../regions/us-central1`` , ``.../zones/us-central1-a`` ,
    ``.../networks/default``). The reconciler only wants the trailing
    identifier, so collapse the link to its basename. ``None`` / empty
    returns ``""``.
    """
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _region_from_zone(zone_url: str | None) -> str:
    """Trim a zone basename (``us-central1-a``) to its region (``us-central1``).

    GCE instances live in zones; we surface the parent region for naming
    parity with the other providers. A zone is the region plus a trailing
    ``-<letter>`` suffix, so drop the last hyphen-delimited segment.
    """
    zone = _basename(zone_url)
    if not zone:
        return ""
    head, _, _tail = zone.rpartition("-")
    return head or zone


class GCPConnector(CloudConnector):
    """Read-only GCP Compute Engine connector (provider ``gcp``)."""

    provider = "gcp"

    # ── Project / region helpers ───────────────────────────────────────

    @property
    def _project_ids(self) -> list[str]:
        ids = self.provider_config.get("project_ids") or []
        return [str(p) for p in ids if p]

    def _region_allowed(self, region: str | None) -> bool:
        """A region passes when no allow-list is set or it is listed."""
        if not self.regions:
            return True
        return (region or "") in self.regions

    # ── Credential + client factory layer (monkeypatched in tests) ──────

    def _parsed_key(self) -> dict[str, Any]:
        """Parse the service-account key JSON string into a dict.

        Raises :class:`CloudConnectorError` on missing / malformed JSON so
        the probe + reconciler surface a clean message instead of a raw
        ``KeyError`` / ``JSONDecodeError``.
        """
        raw = self.credentials.get("service_account_json")
        if not raw:
            raise CloudConnectorError("GCP credentials missing 'service_account_json'.")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CloudConnectorError(f"GCP service_account_json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CloudConnectorError("GCP service_account_json must decode to an object.")
        return data

    def _credentials(self) -> Credentials:
        """Build google-auth service-account credentials from the key dict.

        Lazy SDK import; raises :class:`CloudConnectorError` if the google
        libraries are absent or the key is rejected.
        """
        info = self._parsed_key()
        try:
            from google.oauth2 import service_account
        except ImportError as exc:  # pragma: no cover - optional dependency guard
            raise CloudConnectorError(
                "google-auth is not installed; cannot build GCP credentials."
            ) from exc
        try:
            return service_account.Credentials.from_service_account_info(info)
        except (ValueError, KeyError) as exc:
            raise CloudConnectorError(f"GCP service-account key is invalid: {exc}") from exc

    # One factory per compute client. Each lazy-imports compute_v1 so the
    # module never hard-depends on it and tests patch these directly.

    def _networks_client(self, credentials: Credentials) -> Any:
        from google.cloud import compute_v1

        return compute_v1.NetworksClient(credentials=credentials)

    def _subnetworks_client(self, credentials: Credentials) -> Any:
        from google.cloud import compute_v1

        return compute_v1.SubnetworksClient(credentials=credentials)

    def _instances_client(self, credentials: Credentials) -> Any:
        from google.cloud import compute_v1

        return compute_v1.InstancesClient(credentials=credentials)

    def _addresses_client(self, credentials: Credentials) -> Any:
        from google.cloud import compute_v1

        return compute_v1.AddressesClient(credentials=credentials)

    def _global_addresses_client(self, credentials: Credentials) -> Any:
        from google.cloud import compute_v1

        return compute_v1.GlobalAddressesClient(credentials=credentials)

    def _forwarding_rules_client(self, credentials: Credentials) -> Any:
        from google.cloud import compute_v1

        return compute_v1.ForwardingRulesClient(credentials=credentials)

    def _global_forwarding_rules_client(self, credentials: Credentials) -> Any:
        from google.cloud import compute_v1

        return compute_v1.GlobalForwardingRulesClient(credentials=credentials)

    # ── Probe ──────────────────────────────────────────────────────────

    async def probe(self) -> CloudProbeResult:
        """Cheap auth + reachability check: list networks in the first project.

        Returns ``ok=False`` for any expected failure (bad key, denied
        permission, network error) rather than raising.
        """
        try:
            credentials = self._credentials()
        except CloudConnectorError as exc:
            return CloudProbeResult(ok=False, message=str(exc))

        projects = self._project_ids
        if not projects:
            return CloudProbeResult(ok=False, message="GCP provider_config has no project_ids.")
        first_project = projects[0]

        key = self._parsed_key()
        account_id = key.get("client_email") or first_project

        try:
            client = self._networks_client(credentials)
            networks = await asyncio.to_thread(self._list_networks, client, first_project)
        except Exception as exc:  # noqa: BLE001 - normalise every API/auth fault to ok=False
            return CloudProbeResult(
                ok=False, message=self._describe_error(exc), account_id=account_id
            )

        return CloudProbeResult(
            ok=True,
            message=f"Connected to GCP project {first_project} ({len(networks)} networks).",
            account_id=account_id,
            network_count=len(networks),
        )

    # ── Inventory ──────────────────────────────────────────────────────

    async def fetch_inventory(
        self,
        *,
        include_stopped: bool = False,
        include_load_balancers: bool = True,
    ) -> CloudInventory:
        """Pull the normalised inventory across every configured project.

        A credential-build failure is fatal (raises
        :class:`CloudConnectorError`); a single project's API failure is
        downgraded to a warning so one denied project doesn't abort the
        whole sweep.
        """
        credentials = self._credentials()  # fatal on failure, per contract
        key = self._parsed_key()
        account_id = key.get("client_email") or (self._project_ids[0] if self._project_ids else "")

        inventory = CloudInventory(account_id=account_id)
        if not self._project_ids:
            inventory.warnings.append("GCP provider_config has no project_ids; nothing to sync.")
            return inventory

        for project in self._project_ids:
            try:
                await asyncio.to_thread(
                    self._collect_project,
                    credentials,
                    project,
                    inventory,
                    include_stopped,
                    include_load_balancers,
                )
            except Exception as exc:  # noqa: BLE001 - one project's fault must not abort the rest
                msg = f"GCP project {project}: {self._describe_error(exc)}"
                logger.warning("cloud.gcp.project_failed", project=project, error=str(exc))
                inventory.warnings.append(msg)
                # #430 — mark this scope failed so the reconciler skips the
                # absence-delete pass; a partial walk (subnets landed,
                # instances denied) must not purge the project's rows.
                inventory.failed_scopes.append(f"project {project}")

        return inventory

    # ── Per-project collection (runs in a worker thread) ────────────────

    def _collect_project(
        self,
        credentials: Credentials,
        project: str,
        inventory: CloudInventory,
        include_stopped: bool,
        include_load_balancers: bool,
    ) -> None:
        """Blocking: gather one project's resources and append to inventory.

        Invoked via ``asyncio.to_thread`` from :meth:`fetch_inventory`.
        Network → subnetwork name resolution is local to this project.
        """
        # Networks first so subnets can resolve their parent by name.
        net_by_name: dict[str, CloudNetwork] = {}
        networks_client = self._networks_client(credentials)
        for net in self._list_networks(networks_client, project):
            network = CloudNetwork(id=str(net.id), name=net.name, cidrs=())
            inventory.networks.append(network)
            net_by_name[net.name] = network

        # Subnetworks (regional, aggregated across every region).
        subnets_client = self._subnetworks_client(credentials)
        for sn in self._aggregated_items(subnets_client.aggregated_list(project=project)):
            region = _basename(getattr(sn, "region", None))
            if not self._region_allowed(region):
                continue
            parent_name = _basename(getattr(sn, "network", None))
            if parent_name not in net_by_name:
                inventory.warnings.append(
                    f"GCP project {project}: subnet {sn.name} references unknown "
                    f"network {parent_name!r}; skipped."
                )
                continue
            inventory.subnets.append(
                CloudSubnet(
                    id=str(sn.id),
                    name=sn.name,
                    network_id=net_by_name[parent_name].id,
                    cidr=sn.ip_cidr_range,
                    region=region or None,
                )
            )

        # Instances (zonal, aggregated). region = parent of the zone.
        instances_client = self._instances_client(credentials)
        for inst in self._aggregated_items(instances_client.aggregated_list(project=project)):
            running = getattr(inst, "status", "") == "RUNNING"
            if not running and not include_stopped:
                continue
            region = _region_from_zone(getattr(inst, "zone", None))
            if not self._region_allowed(region):
                continue
            inventory.instances.append(
                CloudInstance(
                    id=str(inst.id),
                    name=inst.name,
                    running=running,
                    region=region or None,
                    nics=self._nics_from_instance(inst),
                )
            )

        # External addresses: regional (aggregated) + global.
        addresses_client = self._addresses_client(credentials)
        for addr in self._aggregated_items(addresses_client.aggregated_list(project=project)):
            self._append_public_ip(addr, inventory)
        global_addresses_client = self._global_addresses_client(credentials)
        for addr in global_addresses_client.list(project=project):
            self._append_public_ip(addr, inventory)

        # Load balancers surface as forwarding rules: regional + global.
        if include_load_balancers:
            fr_client = self._forwarding_rules_client(credentials)
            for fr in self._aggregated_items(fr_client.aggregated_list(project=project)):
                region = _basename(getattr(fr, "region", None)) or "global"
                if region != "global" and not self._region_allowed(region):
                    continue
                inventory.load_balancers.append(self._lb_from_rule(fr, region))
            global_fr_client = self._global_forwarding_rules_client(credentials)
            for fr in global_fr_client.list(project=project):
                inventory.load_balancers.append(self._lb_from_rule(fr, "global"))

    # ── Per-resource shaping ────────────────────────────────────────────

    @staticmethod
    def _list_networks(client: Any, project: str) -> list[Any]:
        """Materialise the (lazy, paginated) networks list for a project."""
        return list(client.list(project=project))

    @staticmethod
    def _aggregated_items(pages: Any) -> list[Any]:
        """Flatten a GCP ``aggregated_list`` response into a flat item list.

        ``aggregated_list`` iterates ``(scope, scoped_list)`` pairs keyed by
        region/zone; each ``scoped_list`` carries the resource list under an
        attribute named after the resource (``subnetworks`` / ``instances`` /
        ``addresses`` / ``forwarding_rules``). We don't know the attribute
        name up front, so pull whichever list-valued attribute the scoped
        list exposes (skipping the ``warning`` field empty scopes carry).
        """
        items: list[Any] = []
        for _scope, scoped_list in pages:
            for attr in ("subnetworks", "instances", "addresses", "forwarding_rules"):
                value = getattr(scoped_list, attr, None)
                if value:
                    items.extend(value)
                    break
        return items

    @staticmethod
    def _nics_from_instance(inst: Any) -> tuple[CloudNic, ...]:
        """Map an instance's network interfaces to :class:`CloudNic` rows.

        ``network_i_p`` is the protobuf snake_case for ``networkIP`` (the
        private address); ``nat_i_p`` likewise for the access-config
        ``natIP`` (the ephemeral/static public address, if any). GCP does
        not surface NIC MAC addresses.
        """
        nics: list[CloudNic] = []
        for ni in getattr(inst, "network_interfaces", None) or ():
            public_ip: str | None = None
            access_configs = getattr(ni, "access_configs", None) or ()
            if access_configs:
                public_ip = getattr(access_configs[0], "nat_i_p", None) or None
            nics.append(
                CloudNic(
                    private_ip=getattr(ni, "network_i_p", None) or "",
                    public_ip=public_ip,
                    mac=None,
                )
            )
        return tuple(nics)

    @staticmethod
    def _append_public_ip(addr: Any, inventory: CloudInventory) -> None:
        """Append a reserved EXTERNAL address; INTERNAL addresses are skipped."""
        if getattr(addr, "address_type", "") != "EXTERNAL":
            return
        inventory.public_ips.append(
            CloudPublicIP(
                address=getattr(addr, "address", "") or "",
                name=getattr(addr, "name", "") or "",
                attached=bool(getattr(addr, "users", None)),
            )
        )

    @staticmethod
    def _lb_from_rule(fr: Any, region: str) -> CloudLoadBalancer:
        """Shape a forwarding rule into a :class:`CloudLoadBalancer`.

        ``i_p_address`` is the protobuf snake_case for the rule's
        ``IPAddress`` frontend.
        """
        frontend = getattr(fr, "i_p_address", None)
        return CloudLoadBalancer(
            id=str(fr.id),
            name=fr.name,
            frontend_ips=(frontend,) if frontend else (),
            region=region or "global",
        )

    # ── Error description ───────────────────────────────────────────────

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        """Render a google-auth / google-api-core fault into a short message.

        Both google exception hierarchies are imported lazily so this stays
        importable without the SDK; if a recognised type matches we keep its
        message, otherwise we fall back to ``str(exc)``.
        """
        try:
            from google.api_core import exceptions as api_exceptions
            from google.auth import exceptions as auth_exceptions
        except ImportError:  # pragma: no cover - optional dependency guard
            return str(exc) or exc.__class__.__name__
        if isinstance(exc, auth_exceptions.GoogleAuthError):
            return f"GCP authentication failed: {exc}"
        if isinstance(exc, api_exceptions.Forbidden):
            return f"GCP permission denied: {exc}"
        if isinstance(exc, api_exceptions.GoogleAPICallError):
            return f"GCP API error: {exc}"
        return str(exc) or exc.__class__.__name__


__all__ = ["GCPConnector"]
