"""Bootstrap-key resolution including pairing-code exchange (#169 Phase 3).

Mirror of ``agent/dns/spatium_dns_agent/pairing.py`` for the DHCP
agent. See that file for the design rationale. The two are kept as
separate per-agent copies (rather than a shared library) because
the agents are independent Python packages with their own
dependency closures + container images.

Resolution priority (first match wins):

1. ``SPATIUM_AGENT_KEY`` env var — the long DHCP bootstrap PSK.
2. Cached resolved key from a previous ``/pair`` call.
3. ``BOOTSTRAP_PAIRING_CODE`` env var → consume against
   ``/api/v1/appliance/pair``, persist + return the DHCP key.

The DHCP agent reads ``bootstrap_keys["dhcp"]`` from the consume
response — works equally for a ``kind='dhcp'`` code (single-key
response) and a ``kind='both'`` code (two-key response, this agent
just uses its half).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

_AGENT_KIND = "dhcp"
_BOOTSTRAP_KEY_FILE = "bootstrap.key"


class PairingError(RuntimeError):
    """Fatal pairing-code failure — invalid / expired / already used."""


def _save_bootstrap_key(state_dir: Path, key: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / _BOOTSTRAP_KEY_FILE
    tmp = path.with_suffix(".key.tmp")
    tmp.write_text(key)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _load_bootstrap_key(state_dir: Path) -> str | None:
    path = state_dir / _BOOTSTRAP_KEY_FILE
    if not path.exists():
        return None
    val = path.read_text().strip()
    return val or None


def _exchange_pairing_code(
    *,
    client: httpx.Client,
    code: str,
    hostname: str,
) -> str:
    resp = client.post(
        "/api/v1/appliance/pair",
        json={"code": code, "hostname": hostname},
    )
    if resp.status_code == 200:
        body = resp.json()
        keys = body.get("bootstrap_keys") or {}
        key = keys.get(_AGENT_KIND)
        if not key:
            kind = body.get("deployment_kind", "<unknown>")
            raise PairingError(
                f"Pairing succeeded but the code's deployment_kind={kind} "
                "doesn't carry a DHCP bootstrap key. Generate a 'dhcp' or "
                "'both' code instead."
            )
        return str(key)
    if resp.status_code in (403, 422):
        raise PairingError(
            f"Pairing code rejected ({resp.status_code}): {resp.text[:200]}. "
            "Regenerate a fresh code on the control plane and re-run."
        )
    resp.raise_for_status()
    raise PairingError(f"Unexpected status {resp.status_code}")


def resolve_bootstrap_key(
    *,
    explicit_key: str,
    pairing_code: str,
    state_dir: Path,
    hostname: str,
    client_factory: Callable[[], httpx.Client],
) -> str:
    if explicit_key:
        return explicit_key

    cached = _load_bootstrap_key(state_dir)
    if cached:
        log.info("dhcp_agent_bootstrap_key_loaded_from_cache")
        return cached

    if not pairing_code:
        raise RuntimeError(
            "One of SPATIUM_AGENT_KEY or BOOTSTRAP_PAIRING_CODE must be "
            "set, and no cached bootstrap key was found on disk."
        )

    with client_factory() as c:
        key = _exchange_pairing_code(client=c, code=pairing_code, hostname=hostname)
    _save_bootstrap_key(state_dir, key)
    log.info("dhcp_agent_bootstrap_key_resolved_from_pairing_code")
    return key
