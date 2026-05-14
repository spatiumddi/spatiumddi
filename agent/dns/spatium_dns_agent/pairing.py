"""Bootstrap-key resolution including pairing-code exchange (#169 Phase 3).

Operators registering a DNS agent today have to paste the long
opaque ``DNS_AGENT_KEY`` into the installer / env file. Phase 3 lets
them paste a short ``BOOTSTRAP_PAIRING_CODE`` instead — the agent
swaps it for the real key on first boot via
``POST /api/v1/appliance/pair``.

Resolution priority (first match wins):

1. ``DNS_AGENT_KEY`` env var if set explicitly. This is the long
   hex string. Highest priority so operators can re-bootstrap with
   the full key without having to clear state_dir first.
2. Cached resolved key from a previous successful ``/pair`` (lives
   at ``<state_dir>/bootstrap.key``, mode 0600). Lets the agent
   survive ``rm /var/lib/.../agent_token.jwt`` without needing a
   fresh pairing code.
3. ``BOOTSTRAP_PAIRING_CODE`` env var → consume against ``/pair``,
   persist the returned key to ``<state_dir>/bootstrap.key``, return
   it. Codes are single-use, so this only succeeds once per code;
   the cache write makes re-bootstrap from disk possible.

A 403 from ``/pair`` is *fatal* by design — the code is invalid,
expired, or already used. The operator needs to generate a new
code. We surface a clear log line + raise so the supervisor exits
non-zero rather than backoff-looping forever on a dead code.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

# kind argument the agent sends to /pair when reading from the
# response's ``bootstrap_keys`` dict. The DNS agent always wants
# the DNS key — even from a ``kind='both'`` code (those still
# carry both DNS + DHCP keys; this agent only consumes its own).
_AGENT_KIND = "dns"

# Filename for the cached resolved key under state_dir. Mode 0600.
_BOOTSTRAP_KEY_FILE = "bootstrap.key"


class PairingError(RuntimeError):
    """Fatal pairing-code failure — invalid / expired / already used.
    The caller propagates this up so the agent exits with a clear
    error instead of backoff-looping against a dead code."""


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
    """POST the code to ``/api/v1/appliance/pair``. Returns the DNS
    bootstrap key on 200; raises ``PairingError`` on 403/422 (dead
    code); re-raises ``httpx.HTTPError`` on transient errors so the
    caller's outer backoff loop retries."""
    resp = client.post(
        "/api/v1/appliance/pair",
        json={"code": code, "hostname": hostname},
    )
    if resp.status_code == 200:
        body = resp.json()
        keys = body.get("bootstrap_keys") or {}
        key = keys.get(_AGENT_KIND)
        if not key:
            # Code was for a DHCP-only deployment — the response
            # doesn't carry a DNS key. Fatal: this DNS agent can't
            # proceed. Operator needs a DNS or 'both' code.
            kind = body.get("deployment_kind", "<unknown>")
            raise PairingError(
                f"Pairing succeeded but the code's deployment_kind={kind} "
                "doesn't carry a DNS bootstrap key. Generate a 'dns' or "
                "'both' code instead."
            )
        return str(key)
    if resp.status_code in (403, 422):
        raise PairingError(
            f"Pairing code rejected ({resp.status_code}): {resp.text[:200]}. "
            "Regenerate a fresh code on the control plane and re-run."
        )
    # Some other unexpected status — let the caller decide whether to
    # retry or bail. Use raise_for_status so httpx surfaces it as an
    # HTTPError that the bootstrap loop already handles.
    resp.raise_for_status()
    # Shouldn't reach here.
    raise PairingError(f"Unexpected status {resp.status_code}")


def resolve_bootstrap_key(
    *,
    explicit_key: str,
    pairing_code: str,
    state_dir: Path,
    hostname: str,
    client_factory: Callable[[], httpx.Client],
) -> str:
    """Pick a bootstrap key per the priority above. ``client_factory``
    builds an ``httpx.Client`` bound to the control-plane URL — we
    only call it if we actually need to hit ``/pair``.

    Returns the key (always non-empty on success). Raises
    ``PairingError`` if every option is exhausted, or ``RuntimeError``
    when none of the inputs are present.
    """
    if explicit_key:
        return explicit_key

    cached = _load_bootstrap_key(state_dir)
    if cached:
        log.info("dns_agent_bootstrap_key_loaded_from_cache")
        return cached

    if not pairing_code:
        raise RuntimeError(
            "One of DNS_AGENT_KEY or BOOTSTRAP_PAIRING_CODE must be set, "
            "and no cached bootstrap key was found on disk."
        )

    with client_factory() as c:
        key = _exchange_pairing_code(client=c, code=pairing_code, hostname=hostname)
    _save_bootstrap_key(state_dir, key)
    log.info("dns_agent_bootstrap_key_resolved_from_pairing_code")
    return key
