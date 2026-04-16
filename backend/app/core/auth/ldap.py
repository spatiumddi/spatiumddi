"""Synchronous LDAP authentication helpers.

Callers must invoke these from a worker thread (``asyncio.to_thread``) — the
underlying ``ldap3`` library is blocking. See
``backend/app/api/v1/auth/router.py`` for the async wrapper used at login.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any

import structlog
from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPException,
    LDAPInvalidCredentialsResult,
    LDAPSocketOpenError,
)
from ldap3.utils.conv import escape_filter_chars

from app.core.auth.user_sync import ExternalAuthResult
from app.core.crypto import decrypt_dict
from app.models.auth_provider import AuthProvider

logger = structlog.get_logger(__name__)


class LDAPServiceError(Exception):
    """Raised when the LDAP server or service-account credentials are
    misconfigured — distinct from a user password being wrong."""


@dataclass
class LDAPConfig:
    host: str
    port: int
    use_ssl: bool
    start_tls: bool
    bind_dn: str
    bind_password: str
    user_base_dn: str
    user_filter: str
    group_base_dn: str
    attr_username: str
    attr_email: str
    attr_display_name: str
    attr_member_of: str
    # Optional CA cert file for TLS validation (container path). None = system store.
    tls_ca_cert_file: str | None = None

    @classmethod
    def from_provider(cls, provider: AuthProvider) -> "LDAPConfig":
        cfg = provider.config or {}
        secrets = (
            decrypt_dict(provider.secrets_encrypted) if provider.secrets_encrypted else {}
        )

        host = str(cfg.get("host") or "").strip()
        if not host:
            raise LDAPServiceError("config.host is required")
        bind_dn = str(cfg.get("bind_dn") or "").strip()
        bind_password = str(secrets.get("bind_password") or "")
        if not bind_dn or not bind_password:
            raise LDAPServiceError("config.bind_dn and secrets.bind_password are required")
        user_base_dn = str(cfg.get("user_base_dn") or "").strip()
        if not user_base_dn:
            raise LDAPServiceError("config.user_base_dn is required")
        user_filter = str(cfg.get("user_filter") or "").strip()
        if "{username}" not in user_filter:
            raise LDAPServiceError(
                "config.user_filter must contain the {username} placeholder"
            )

        use_ssl = bool(cfg.get("use_ssl", True))
        port_raw = cfg.get("port")
        port = int(port_raw) if port_raw is not None else (636 if use_ssl else 389)

        return cls(
            host=host,
            port=port,
            use_ssl=use_ssl,
            start_tls=bool(cfg.get("start_tls", False)),
            bind_dn=bind_dn,
            bind_password=bind_password,
            user_base_dn=user_base_dn,
            user_filter=user_filter,
            group_base_dn=str(cfg.get("group_base_dn") or "").strip(),
            attr_username=str(cfg.get("attr_username") or "sAMAccountName"),
            attr_email=str(cfg.get("attr_email") or "mail"),
            attr_display_name=str(cfg.get("attr_display_name") or "displayName"),
            attr_member_of=str(cfg.get("attr_member_of") or "memberOf"),
            tls_ca_cert_file=(cfg.get("tls_ca_cert_file") or None),
        )


def _server(cfg: LDAPConfig) -> Server:
    tls: Tls | None = None
    if cfg.use_ssl or cfg.start_tls:
        tls = Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=cfg.tls_ca_cert_file,
            version=ssl.PROTOCOL_TLS_CLIENT,
        )
    return Server(
        cfg.host,
        port=cfg.port,
        use_ssl=cfg.use_ssl,
        tls=tls,
        get_info=ALL,
        connect_timeout=5,
    )


def _open(cfg: LDAPConfig, user: str, password: str) -> Connection:
    """Open + bind a connection. Raises LDAPBindError on credential failure,
    LDAPSocketOpenError / LDAPException on infrastructure failure."""
    conn = Connection(
        _server(cfg),
        user=user,
        password=password,
        auto_bind=False,
        raise_exceptions=True,
        receive_timeout=10,
    )
    if cfg.start_tls and not cfg.use_ssl:
        conn.open()
        conn.start_tls()
    conn.bind()
    return conn


def _user_attrs(cfg: LDAPConfig) -> list[str]:
    return [
        cfg.attr_username,
        cfg.attr_email,
        cfg.attr_display_name,
        cfg.attr_member_of,
    ]


def _attr_first(entry: Any, name: str) -> str | None:
    """Pull a single string from an ldap3 entry attribute, which may be a list."""
    val = entry.get(name) if hasattr(entry, "get") else None
    if val is None:
        return None
    if isinstance(val, list):
        return str(val[0]) if val else None
    return str(val)


def _attr_list(entry: Any, name: str) -> list[str]:
    val = entry.get(name) if hasattr(entry, "get") else None
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val]
    return [str(val)]


def _search_user(conn: Connection, cfg: LDAPConfig, username: str) -> dict | None:
    """Returns a dict like {'dn': ..., 'attributes': {...}} or None if not found."""
    filt = cfg.user_filter.format(username=escape_filter_chars(username))
    conn.search(
        search_base=cfg.user_base_dn,
        search_filter=filt,
        search_scope=SUBTREE,
        attributes=_user_attrs(cfg),
        size_limit=2,
    )
    entries = conn.response or []
    hits = [e for e in entries if e.get("type") == "searchResEntry"]
    if not hits:
        return None
    if len(hits) > 1:
        raise LDAPServiceError(
            f"user_filter matched {len(hits)} entries for {username!r} — refine the filter"
        )
    hit = hits[0]
    return {"dn": hit["dn"], "attributes": hit.get("attributes") or {}}


def _result_from_entry(cfg: LDAPConfig, dn: str, attrs: dict) -> ExternalAuthResult:
    return ExternalAuthResult(
        external_id=dn,
        username=(_attr_first(attrs, cfg.attr_username) or "").strip(),
        email=_attr_first(attrs, cfg.attr_email),
        display_name=_attr_first(attrs, cfg.attr_display_name),
        groups=_attr_list(attrs, cfg.attr_member_of),
    )


def authenticate_ldap(
    provider: AuthProvider, username: str, password: str
) -> ExternalAuthResult | None:
    """Attempt to authenticate ``username``/``password`` against ``provider``.

    Returns an ``ExternalAuthResult`` on success, ``None`` on bad credentials.
    Raises ``LDAPServiceError`` on configuration / connection failure so the
    caller can fall through to the next provider.
    """
    if not password:
        return None

    cfg = LDAPConfig.from_provider(provider)

    # 1) Service bind + user search.
    try:
        service = _open(cfg, cfg.bind_dn, cfg.bind_password)
    except LDAPInvalidCredentialsResult as exc:
        raise LDAPServiceError(f"service bind rejected: {exc}") from exc
    except LDAPBindError as exc:
        raise LDAPServiceError(f"service bind failed: {exc}") from exc
    except LDAPSocketOpenError as exc:
        raise LDAPServiceError(f"cannot reach LDAP server: {exc}") from exc
    except LDAPException as exc:
        raise LDAPServiceError(f"LDAP error during service bind: {exc}") from exc

    try:
        found = _search_user(service, cfg, username)
    finally:
        service.unbind()

    if found is None:
        return None

    dn = found["dn"]
    attrs = found["attributes"]

    # 2) Bind as the user to verify the password.
    try:
        user_conn = _open(cfg, dn, password)
    except (LDAPInvalidCredentialsResult, LDAPBindError):
        return None
    except LDAPSocketOpenError as exc:
        raise LDAPServiceError(f"cannot reach LDAP server: {exc}") from exc
    except LDAPException as exc:
        raise LDAPServiceError(f"LDAP error during user bind: {exc}") from exc
    user_conn.unbind()

    return _result_from_entry(cfg, dn, attrs)


def test_connection(
    provider: AuthProvider, username: str | None = None, password: str | None = None
) -> dict[str, Any]:
    """Structured probe for the admin "Test connection" button.

    Never raises — returns ``{ok, message, details}``.
    """
    try:
        cfg = LDAPConfig.from_provider(provider)
    except LDAPServiceError as exc:
        return {"ok": False, "message": str(exc), "details": {}}

    try:
        service = _open(cfg, cfg.bind_dn, cfg.bind_password)
    except (LDAPInvalidCredentialsResult, LDAPBindError) as exc:
        return {"ok": False, "message": f"service bind rejected: {exc}", "details": {}}
    except LDAPSocketOpenError as exc:
        return {"ok": False, "message": f"cannot reach server: {exc}", "details": {}}
    except LDAPException as exc:
        return {"ok": False, "message": f"LDAP error: {exc}", "details": {}}

    try:
        if username is None:
            return {
                "ok": True,
                "message": "service bind OK",
                "details": {
                    "server": f"{cfg.host}:{cfg.port}",
                    "tls": "ssl" if cfg.use_ssl else ("starttls" if cfg.start_tls else "plain"),
                },
            }

        found = _search_user(service, cfg, username)
        if found is None:
            return {
                "ok": False,
                "message": f"user {username!r} not found with user_filter",
                "details": {},
            }
        dn = found["dn"]
        attrs = found["attributes"]
        result = _result_from_entry(cfg, dn, attrs)

        details: dict[str, Any] = {
            "dn": result.external_id,
            "username": result.username,
            "email": result.email,
            "display_name": result.display_name,
            "group_dns": result.groups,
        }

        if password:
            try:
                user_conn = _open(cfg, dn, password)
                user_conn.unbind()
                details["user_bind"] = "ok"
            except (LDAPInvalidCredentialsResult, LDAPBindError):
                return {
                    "ok": False,
                    "message": "user password rejected",
                    "details": details,
                }

        return {"ok": True, "message": "OK", "details": details}
    finally:
        service.unbind()
