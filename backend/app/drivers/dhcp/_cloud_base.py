"""Shared base for agentless external DHCP drivers.

The DHCP subsystem started agent-only (Kea long-polls a ``ConfigBundle``),
plus ``windows_dhcp`` which is agentless but drives the server per-object
over WinRM. This module adds the first *cloud/REST* agentless DHCP shape:
the control plane speaks a provider's HTTP API directly with an API token,
exactly like the agentless cloud DNS drivers (see
:mod:`app.drivers.dns._cloud_base`).

The first concrete provider is FortiGate (see ``fortigate.py``). A
FortiGate is interface-bound — one ``system.dhcp.server`` object per
interface/subnet — so the natural write unit is **the whole DHCP-server
object per scope**: any scope / pool / static / option edit rebuilds that
scope's full desired object from the DB and pushes it whole (create if
absent). That is why this base exposes *whole-scope* writes
(``apply_scope_full`` / ``remove_scope_full``) rather than the per-object
``apply_reservation`` / ``apply_exclusion`` methods the Windows driver
uses. The write-through service (``services.dhcp.windows_writethrough``)
detects a :class:`AgentlessDHCPDriverBase` member and re-pushes the whole
scope on every edit.

**Contract, mirroring the cloud DNS base:**

* No ConfigBundle / long-poll. ``render_config`` / ``apply_config`` raise
  (agentless drivers never render daemon config); ``reload`` / ``restart``
  are no-ops; ``validate_config`` accepts anything.
* Credentials live Fernet-encrypted in the existing
  ``DHCPServer.credentials_encrypted`` column — no new credential store.
* Writes run synchronously from the control plane, before commit, so a
  REST failure surfaces as a 502 and rolls the transaction back.
* Lease reads use ``get_leases`` (the same method name the Kea / Windows
  drivers expose) so the existing ``pull_leases`` path works unchanged.

A concrete provider subclasses :class:`AgentlessDHCPDriverBase` and
implements only the provider hooks (``_apply_scope``, ``_remove_scope``,
``_get_leases``, ``_probe``, ``capabilities``) plus the ``name`` /
``credential_fields`` class attrs. Everything ``DHCPDriver``-shaped is
handled here.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.crypto import decrypt_dict
from app.drivers.dhcp.base import ConfigBundle, DHCPDriver, ScopeDef

logger = structlog.get_logger(__name__)


class CloudDHCPError(Exception):
    """Auth / API / config failure from an agentless DHCP provider call.

    Raised so the write-through + probe paths surface a clean
    operator-facing message instead of a raw HTTP/SDK traceback.
    """


@dataclass(frozen=True)
class CloudDHCPProbe:
    """Result of an agentless DHCP credential probe (Test-connection button)."""

    ok: bool
    message: str
    interface_count: int | None = None


class AgentlessDHCPDriverBase(DHCPDriver):
    """Agentless base for external DHCP providers driven over a REST API."""

    name: str = "cloud_dhcp"
    # Ordered credential field names the create modal renders + the probe
    # validates. Providers override (e.g. FortiGate → api_token, vdom).
    credential_fields: tuple[str, ...] = ()

    # ── Agentless no-ops / rejections (control plane talks to the API) ──
    def render_config(self, bundle: ConfigBundle) -> str:
        raise NotImplementedError(f"{self.name} is agentless; there is no daemon config to render")

    async def apply_config(self, server: Any, bundle: ConfigBundle) -> None:
        raise NotImplementedError(
            f"{self.name} is agentless; config is pushed per-scope, not as a bundle"
        )

    async def reload(self, server: Any) -> None:
        return

    async def restart(self, server: Any) -> None:
        return

    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        # No daemon config is rendered for an agentless provider; accept
        # anything so upstream validators don't block.
        return (True, [])

    # ── Credential handling ─────────────────────────────────────────────
    def _load_credentials(self, server: Any) -> dict[str, Any]:
        """Decrypt the provider credential dict from the server row.

        Raises :class:`CloudDHCPError` when unset so the caller can tell
        "not configured" apart from "API rejected the token".
        """
        blob = getattr(server, "credentials_encrypted", None)
        if not blob:
            raise CloudDHCPError(
                f"DHCP server {getattr(server, 'name', '<unknown>')!r} has no "
                f"{self.name} credentials configured."
            )
        try:
            return decrypt_dict(blob)
        except ValueError as exc:
            raise CloudDHCPError(f"{self.name} credentials could not be decrypted: {exc}") from exc

    # ── Whole-scope write path (synchronous, control-plane) ─────────────
    async def apply_scope_full(self, server: Any, scope: ScopeDef) -> None:
        """Push one scope's full desired DHCP config to the provider.

        Called by ``windows_writethrough`` after a scope / pool / static /
        option edit (and on ``/sync``). The provider create-or-updates the
        object that serves ``scope.subnet_cidr``. Failures raise
        :class:`CloudDHCPError` so the caller rolls back with a 502.
        """
        creds = self._load_credentials(server)
        await self._apply_scope(server, creds, scope)
        logger.info(
            "cloud_dhcp.apply_scope",
            driver=self.name,
            server=str(getattr(server, "id", "")),
            subnet=scope.subnet_cidr,
            pools=len(scope.pools),
            statics=len(scope.statics),
        )

    async def remove_scope_full(self, server: Any, subnet_cidr: str) -> None:
        """Delete the provider DHCP object serving ``subnet_cidr``."""
        creds = self._load_credentials(server)
        await self._remove_scope(server, creds, subnet_cidr)
        logger.info(
            "cloud_dhcp.remove_scope",
            driver=self.name,
            server=str(getattr(server, "id", "")),
            subnet=subnet_cidr,
        )

    # ── Lease read (plugs into services.dhcp.pull_leases) ───────────────
    async def get_leases(self, server: Any) -> list[dict[str, Any]]:
        creds = self._load_credentials(server)
        return await self._get_leases(server, creds)

    # NOTE: ``get_scopes`` is intentionally NOT implemented — these drivers
    # are push-only (SpatiumDDI is the source of truth), so the pull_leases
    # Phase-1 scope-upsert (gated on ``hasattr(driver, "get_scopes")``) is
    # skipped and only Phase-2 lease reconcile runs.

    # ── Connection test / health ────────────────────────────────────────
    async def probe(self, server: Any) -> CloudDHCPProbe:
        """Cheap credential check for the Test-connection button.

        Never raises for an expected failure — returns ``ok=False`` with
        the provider message.
        """
        try:
            creds = self._load_credentials(server)
            return await self._probe(server, creds)
        except CloudDHCPError as exc:
            return CloudDHCPProbe(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001 — surface any transport error cleanly
            return CloudDHCPProbe(ok=False, message=f"{self.name} API error: {exc}")

    async def health_check(self, server: Any) -> tuple[bool, str]:
        result = await self.probe(server)
        return result.ok, result.message

    # ── Provider hooks (subclasses implement) ───────────────────────────
    @abstractmethod
    async def _apply_scope(self, server: Any, creds: dict[str, Any], scope: ScopeDef) -> None:
        """Create / update the provider DHCP object for ``scope``."""

    @abstractmethod
    async def _remove_scope(self, server: Any, creds: dict[str, Any], subnet_cidr: str) -> None:
        """Delete the provider DHCP object serving ``subnet_cidr`` (idempotent)."""

    @abstractmethod
    async def _get_leases(self, server: Any, creds: dict[str, Any]) -> list[dict[str, Any]]:
        """Return active leases as neutral dicts (same shape as Kea/Windows)."""

    @abstractmethod
    async def _probe(self, server: Any, creds: dict[str, Any]) -> CloudDHCPProbe:
        """Authenticate + report reachability for the Test-connection button."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        """Driver capability dict (see ``windows.py`` for the shape)."""


__all__ = [
    "AgentlessDHCPDriverBase",
    "CloudDHCPError",
    "CloudDHCPProbe",
]
