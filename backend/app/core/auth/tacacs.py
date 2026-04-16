"""Synchronous TACACS+ authentication helpers.

Callers must invoke these from a worker thread (``asyncio.to_thread``) — the
underlying ``tacacs_plus`` library does blocking TCP I/O. See
``backend/app/api/v1/auth/router.py`` for the async wrapper used at login.

TACACS+ authorization is a separate round-trip: after ``authenticate()``
returns valid, we call ``authorize()`` with an empty arg list and extract
AV-pairs from the response. The ``priv-lvl`` AV pair is conventional; admins
can also populate a custom ``group`` pair on the TACACS+ server.

Manual test recipe (requires a reachable TACACS+ server):
    $ python -c "from app.core.auth.tacacs import authenticate_tacacs; \\
                  from app.models.auth_provider import AuthProvider; \\
                  p = AuthProvider(config={'server':'10.0.0.1'}, \\
                                   secrets_encrypted=<fernet>); \\
                  print(authenticate_tacacs(p, 'alice', 'secret'))"
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any

import structlog
from tacacs_plus.client import TACACSClient

from app.core.auth.user_sync import ExternalAuthResult
from app.core.crypto import decrypt_dict
from app.models.auth_provider import AuthProvider

logger = structlog.get_logger(__name__)


class TACACSServiceError(Exception):
    """Raised when the TACACS+ server is unreachable or the shared secret is
    misconfigured — distinct from an authentication failure (bad password)."""


@dataclass
class TACACSConfig:
    server: str
    port: int
    secret: str
    timeout: int
    # Name of the AV-pair that carries group info (default ``priv-lvl``).
    # When the value is numeric we emit ``"priv-lvl:<n>"`` so it can be mapped
    # via the group-mapping UI.
    attr_groups: str

    @classmethod
    def from_provider(cls, provider: AuthProvider) -> "TACACSConfig":
        cfg = provider.config or {}
        secrets = (
            decrypt_dict(provider.secrets_encrypted) if provider.secrets_encrypted else {}
        )

        server = str(cfg.get("server") or "").strip()
        if not server:
            raise TACACSServiceError("config.server is required")
        secret_s = str(secrets.get("secret") or "")
        if not secret_s:
            raise TACACSServiceError("secrets.secret is required")

        port_raw = cfg.get("port")
        port = int(port_raw) if port_raw is not None else 49
        timeout_raw = cfg.get("timeout")
        timeout = int(timeout_raw) if timeout_raw is not None else 5

        return cls(
            server=server,
            port=port,
            secret=secret_s,
            timeout=timeout,
            attr_groups=str(cfg.get("attr_groups") or "priv-lvl"),
        )


def _client(cfg: TACACSConfig) -> TACACSClient:
    # family=AF_INET — explicit; library default is AF_UNSPEC which triggers
    # getaddrinfo. Leave to default for IPv6 compatibility.
    return TACACSClient(
        host=cfg.server,
        port=cfg.port,
        secret=cfg.secret,
        timeout=cfg.timeout,
    )


def _extract_groups(args: Any, attr_name: str) -> list[str]:
    """Turn a list of TACACS+ AV-pairs (``name=value`` or ``name*value``) into
    group identifiers. If ``attr_name`` is ``priv-lvl`` and the value is
    numeric, prefix with ``priv-lvl:`` for admin readability in the mapping UI.
    """
    if not args:
        return []
    attr_lc = attr_name.lower()
    out: list[str] = []
    for pair in args:
        if isinstance(pair, bytes):
            pair = pair.decode(errors="replace")
        elif not isinstance(pair, str):
            pair = str(pair)
        # Separator is '=' (mandatory) or '*' (optional) per RFC 8907.
        for sep in ("=", "*"):
            if sep in pair:
                k, _, v = pair.partition(sep)
                break
        else:
            continue
        if k.lower() != attr_lc:
            continue
        v = v.strip()
        if not v:
            continue
        # Emit "priv-lvl:N" for priv-lvl so admins can map e.g. "priv-lvl:15"
        # → Admins in the group-mapping UI.
        if attr_lc == "priv-lvl" and v.isdigit():
            out.append(f"priv-lvl:{v}")
        else:
            out.append(v)
    return out


def authenticate_tacacs(
    provider: AuthProvider, username: str, password: str
) -> ExternalAuthResult | None:
    """Attempt to authenticate ``username``/``password`` against ``provider``.

    Returns an ``ExternalAuthResult`` on auth success, ``None`` on auth
    failure (bad password or user unknown). Raises ``TACACSServiceError``
    on configuration / network failure so the caller can fall through to
    the next provider.
    """
    if not password:
        return None

    cfg = TACACSConfig.from_provider(provider)
    client = _client(cfg)

    try:
        auth_reply = client.authenticate(username, password)
    except (socket.timeout, ConnectionError, OSError) as exc:
        raise TACACSServiceError(f"cannot reach TACACS+ server: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — bad shared-secret / protocol
        raise TACACSServiceError(f"TACACS+ protocol error: {exc}") from exc

    if not getattr(auth_reply, "valid", False):
        return None

    # Authorization is optional but typical — used to fetch AV pairs for
    # group mapping. Failure here is non-fatal; authenticate already succeeded.
    groups: list[str] = []
    try:
        author_reply = client.authorize(username, arguments=[])
        if getattr(author_reply, "valid", False):
            args = getattr(author_reply, "arguments", None) or []
            groups = _extract_groups(args, cfg.attr_groups)
    except Exception as exc:  # noqa: BLE001 — no-authorize TACACS+ servers exist
        logger.warning(
            "tacacs_authorize_failed",
            provider=provider.name,
            username=username,
            error=str(exc),
        )

    return ExternalAuthResult(
        external_id=f"{provider.id}:{username}",
        username=username,
        email=None,
        display_name=username,
        groups=groups,
    )


def test_connection(provider: AuthProvider) -> dict[str, Any]:
    """Structured probe for the admin "Test connection" button.

    Attempts to authenticate a stub user. A returned ``valid=False`` proves
    the server is reachable and the shared secret matches (the failure is
    for the bogus credentials). Network / secret errors raise
    ``TACACSServiceError`` and are caught here.

    Never raises — always returns ``{ok, message, details}``.
    """
    try:
        cfg = TACACSConfig.from_provider(provider)
    except TACACSServiceError as exc:
        return {"ok": False, "message": str(exc), "details": {}}

    probe_user = "__spatium_probe__"
    probe_pass = "__probe__"
    try:
        result = authenticate_tacacs(provider, probe_user, probe_pass)
    except TACACSServiceError as exc:
        return {
            "ok": False,
            "message": str(exc),
            "details": {"server": f"{cfg.server}:{cfg.port}"},
        }

    return {
        "ok": True,
        "message": (
            "server reachable, shared secret OK (stub user unexpectedly accepted)"
            if result is not None
            else "server reachable, shared secret OK (stub user rejected)"
        ),
        "details": {"server": f"{cfg.server}:{cfg.port}"},
    }
