"""External forwarding for audit-log events.

Subscribes to the SQLAlchemy ``after_commit`` lifecycle on every async
session and, for each successfully committed ``AuditLog`` row, fans
the event out to every enabled ``AuditForwardTarget`` using that
target's configured output format and transport.

Supported output formats (syslog kind):

* ``rfc5424_json``  RFC 5424 envelope, JSON body. Default — most
  modern SIEMs (Splunk, Elastic, Graylog) auto-parse embedded JSON.
* ``rfc5424_cef``   RFC 5424 envelope, CEF 0 body. ArcSight + many
  commercial SIEMs.
* ``rfc5424_leef``  RFC 5424 envelope, LEEF 2.0 body. IBM QRadar.
* ``rfc3164``       Legacy BSD syslog — short PRI + timestamp + host
  + tag. For collectors that don't speak 5424.
* ``json_lines``    No syslog wrapper, just one JSON object per line.
  For raw TCP/UDP inputs on Logstash / Fluentd / Vector.

Webhook targets always deliver compact JSON (the HTTP body); the
``format`` column is ignored for ``kind="webhook"``.

Design notes:

* **Never blocks the commit.** The hook collects audit rows inside
  ``after_flush`` while they still have IDs, then schedules delivery
  in ``after_commit`` via ``asyncio.create_task``.
* **One task per target per row.** A dead collector isolates to its
  own target; others still see the event.
* **Legacy flat-config fallback.** When no ``AuditForwardTarget``
  rows exist (fresh install + operator hasn't migrated), we still
  read the flat ``audit_forward_*`` columns on ``PlatformSettings``
  so existing deployments keep working through the upgrade.

See ``docs/OBSERVABILITY.md`` for the operator-facing view.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings as _app_settings
from app.models.audit import AuditLog
from app.models.audit_forward import AuditForwardTarget
from app.models.settings import PlatformSettings

logger = structlog.get_logger(__name__)

# ── RFC 5424 constants ─────────────────────────────────────────────────────

# Severity: 0=emerg, 1=alert, 2=crit, 3=err, 4=warn, 5=notice, 6=info, 7=debug
_SEVERITY_SUCCESS = 6  # info
_SEVERITY_DENIED = 4  # warning
_SEVERITY_FAILED = 3  # err

_APP_NAME = "spatiumddi"
_MSG_ID = "AUDIT"

_SINGLETON_ID = 1

_PENDING_ATTR = "__spatium_audit_forward_pending__"

# min_severity filter — higher rank means more severe. Keeps the
# filter logic a single numeric compare.
_SEVERITY_RANK = {
    "info": 0,
    "warn": 1,
    "error": 2,
    "denied": 3,
}


# ── Event payload ──────────────────────────────────────────────────────────


def _serialize(row: AuditLog) -> dict[str, Any]:
    """Neutral JSON shape — consumed by every formatter + the webhook."""
    ts = getattr(row, "timestamp", None) or datetime.now(UTC)
    return {
        "id": str(row.id),
        "timestamp": ts.isoformat(),
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "resource_display": row.resource_display,
        "result": row.result,
        "user_id": str(row.user_id) if row.user_id else None,
        "user_display_name": row.user_display_name,
        "auth_source": row.auth_source,
        "changed_fields": row.changed_fields or [],
        "old_value": row.old_value,
        "new_value": row.new_value,
    }


def _severity_for_result(result: str | None) -> int:
    r = (result or "").lower()
    if r == "denied":
        return _SEVERITY_DENIED
    if r in ("failed", "error"):
        return _SEVERITY_FAILED
    return _SEVERITY_SUCCESS


def _severity_bucket(result: str | None) -> str:
    r = (result or "").lower()
    if r == "denied":
        return "denied"
    if r in ("failed", "error"):
        return "error"
    return "info"


def _hostname() -> str:
    return socket.gethostname() or "spatiumddi"


# ── Formatters ─────────────────────────────────────────────────────────────


def _render_rfc5424_prefix(facility: int, severity: int, ts: str) -> str:
    pri = (facility << 3) | severity
    return f"<{pri}>1 {ts} {_hostname()} {_APP_NAME} - {_MSG_ID} -"


def _render_rfc5424_json(facility: int, payload: dict[str, Any]) -> str:
    severity = _severity_for_result(payload.get("result"))
    prefix = _render_rfc5424_prefix(facility, severity, payload["timestamp"])
    return prefix + " " + json.dumps(payload, separators=(",", ":"), default=str)


def _cef_escape(s: Any) -> str:
    """Escape a CEF extension value.

    CEF reserves ``\\`` and ``=`` in extension values and ``|`` in the
    header pipe-separated fields. Per the spec any value containing
    those characters must be backslash-escaped.
    """
    v = "" if s is None else str(s)
    return v.replace("\\", "\\\\").replace("=", "\\=").replace("\n", " ").replace("\r", " ")


def _cef_header_escape(s: Any) -> str:
    v = "" if s is None else str(s)
    return v.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _render_cef(payload: dict[str, Any]) -> str:
    """ArcSight CEF 0 body.

    Fixed header: ``CEF:0|Vendor|Product|Version|SignatureID|Name|Severity``
    then key=value extension pairs. CEF severity is 0-10; we map our
    three buckets to 3/6/9 for info/error/denied.
    """
    sev_map = {"info": 3, "error": 6, "denied": 9}
    sev = sev_map[_severity_bucket(payload.get("result"))]
    action = payload.get("action") or "audit"
    resource_type = payload.get("resource_type") or ""
    signature = f"{resource_type}:{action}" if resource_type else action
    name = payload.get("resource_display") or signature

    header = "|".join(
        _cef_header_escape(x)
        for x in [
            "CEF:0",
            "SpatiumDDI",
            "SpatiumDDI",
            "1.0",
            signature,
            name,
            str(sev),
        ]
    )

    ext_fields: list[tuple[str, Any]] = [
        ("act", payload.get("action")),
        ("outcome", payload.get("result")),
        ("suser", payload.get("user_display_name")),
        ("duser", payload.get("resource_display")),
        ("cs1Label", "resource_type"),
        ("cs1", payload.get("resource_type")),
        ("cs2Label", "resource_id"),
        ("cs2", payload.get("resource_id")),
        ("cs3Label", "auth_source"),
        ("cs3", payload.get("auth_source")),
        ("cs4Label", "changed_fields"),
        ("cs4", ",".join(payload.get("changed_fields") or [])),
        ("externalId", payload.get("id")),
        ("rt", payload.get("timestamp")),
    ]
    ext = " ".join(f"{k}={_cef_escape(v)}" for k, v in ext_fields if v not in (None, ""))
    return f"{header}|{ext}"


def _render_rfc5424_cef(facility: int, payload: dict[str, Any]) -> str:
    severity = _severity_for_result(payload.get("result"))
    prefix = _render_rfc5424_prefix(facility, severity, payload["timestamp"])
    return prefix + " " + _render_cef(payload)


def _leef_escape(s: Any) -> str:
    v = "" if s is None else str(s)
    # LEEF uses tab as the default delimiter between key=value pairs, so
    # strip tabs from values. Backslash + = escape like CEF.
    return v.replace("\\", "\\\\").replace("=", "\\=").replace("\t", " ").replace("\n", " ")


def _render_leef(payload: dict[str, Any]) -> str:
    """IBM QRadar LEEF 2.0 body.

    ``LEEF:2.0|Vendor|Product|Version|EventID|DelimiterChar|key=val<delim>…``
    We use ``^`` as the delimiter (DelimiterChar hex ``5e``) because tab
    gets mangled over UDP on some relays.
    """
    action = payload.get("action") or "audit"
    resource_type = payload.get("resource_type") or ""
    event_id = f"{resource_type}:{action}" if resource_type else action

    header = "|".join(
        _leef_escape(x) for x in ["LEEF:2.0", "SpatiumDDI", "SpatiumDDI", "1.0", event_id, "^"]
    )

    fields: list[tuple[str, Any]] = [
        ("devTime", payload.get("timestamp")),
        ("devTimeFormat", "yyyy-MM-dd'T'HH:mm:ss.SSSXXX"),
        ("act", payload.get("action")),
        ("outcome", payload.get("result")),
        ("usrName", payload.get("user_display_name")),
        ("userId", payload.get("user_id")),
        ("resourceType", payload.get("resource_type")),
        ("resourceId", payload.get("resource_id")),
        ("resource", payload.get("resource_display")),
        ("authSource", payload.get("auth_source")),
        ("changedFields", ",".join(payload.get("changed_fields") or [])),
        ("externalId", payload.get("id")),
    ]
    body = "^".join(f"{k}={_leef_escape(v)}" for k, v in fields if v not in (None, ""))
    return f"{header}|{body}"


def _render_rfc5424_leef(facility: int, payload: dict[str, Any]) -> str:
    severity = _severity_for_result(payload.get("result"))
    prefix = _render_rfc5424_prefix(facility, severity, payload["timestamp"])
    return prefix + " " + _render_leef(payload)


def _render_rfc3164(facility: int, payload: dict[str, Any]) -> str:
    """Legacy BSD syslog per RFC 3164.

    ``<PRI>Mmm dd HH:MM:SS host tag: msg``. Month/day/time are in the
    local system's convention — no year, no timezone. Body is compact
    JSON (keeps parsing simple for legacy collectors that index via
    regex).
    """
    severity = _severity_for_result(payload.get("result"))
    pri = (facility << 3) | severity
    try:
        ts = datetime.fromisoformat(payload["timestamp"])
    except (KeyError, ValueError, TypeError):
        ts = datetime.now(UTC)
    # RFC 3164: single-digit days get a leading space, not zero.
    day = f"{ts.day:>2}"
    stamp = ts.strftime(f"%b {day} %H:%M:%S")
    body = json.dumps(payload, separators=(",", ":"), default=str)
    return f"<{pri}>{stamp} {_hostname()} {_APP_NAME}: {body}"


def _render_json_lines(payload: dict[str, Any]) -> str:
    """Bare JSON — no syslog framing. For raw TCP/UDP Logstash / Vector."""
    return json.dumps(payload, separators=(",", ":"), default=str)


_FORMATTERS = {
    "rfc5424_json": _render_rfc5424_json,
    "rfc5424_cef": _render_rfc5424_cef,
    "rfc5424_leef": _render_rfc5424_leef,
    "rfc3164": _render_rfc3164,
    # json_lines takes no facility — adapter below.
}


def render_for_target(fmt: str, facility: int, payload: dict[str, Any]) -> str:
    if fmt == "json_lines":
        return _render_json_lines(payload)
    formatter = _FORMATTERS.get(fmt)
    if formatter is None:
        formatter = _FORMATTERS["rfc5424_json"]
    return formatter(facility, payload)


# Legacy single-format helper — kept so alerts.py doesn't break mid-refactor.
def _render_rfc5424(facility: int, row: Any, payload: dict[str, Any]) -> str:
    return _render_rfc5424_json(facility, payload)


# ── Transport ──────────────────────────────────────────────────────────────


async def _send_syslog(
    host: str,
    port: int,
    protocol: str,
    message: str,
    ca_cert_pem: str | None = None,
) -> None:
    if protocol == "udp":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto((message + "\n").encode("utf-8"), (host, port))
        return

    ssl_ctx: ssl.SSLContext | None = None
    if protocol == "tls":
        if ca_cert_pem:
            ssl_ctx = ssl.create_default_context(cadata=ca_cert_pem)
        else:
            ssl_ctx = ssl.create_default_context()

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_ctx),
        timeout=5.0,
    )
    try:
        writer.write((message + "\n").encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        del reader


async def _send_webhook(url: str, auth_header: str, payload: dict[str, Any]) -> None:
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 300:
            logger.warning(
                "audit_forward_webhook_non2xx",
                status=resp.status_code,
                body_preview=resp.text[:200],
            )


# ── Per-target delivery ────────────────────────────────────────────────────


def _target_accepts(target: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Filter gate: honour min_severity + resource_types on the target."""
    ms = target.get("min_severity")
    if ms:
        needed = _SEVERITY_RANK.get(ms.lower())
        got = _SEVERITY_RANK.get(_severity_bucket(payload.get("result")))
        if needed is not None and got is not None and got < needed:
            return False
    rtypes = target.get("resource_types") or []
    if rtypes and payload.get("resource_type") not in rtypes:
        return False
    return True


async def _deliver_to_target(target: dict[str, Any], payload: dict[str, Any]) -> None:
    if not _target_accepts(target, payload):
        return
    kind = target.get("kind")
    try:
        if kind == "syslog":
            message = render_for_target(
                target.get("format", "rfc5424_json"),
                int(target.get("facility", 16)),
                payload,
            )
            await _send_syslog(
                target["host"],
                int(target["port"]),
                target.get("protocol", "udp"),
                message,
                ca_cert_pem=target.get("ca_cert_pem"),
            )
        elif kind == "webhook":
            await _send_webhook(
                target["url"],
                target.get("auth_header") or "",
                payload,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit_forward_target_failed",
            target=target.get("name"),
            kind=kind,
            error=str(exc),
        )


# ── Legacy deliver helper (alerts.py still calls _deliver_one indirectly) ──


async def _deliver_one(
    payload: dict[str, Any],
    row_summary: dict[str, Any],  # noqa: ARG001 — retained for call compat
    syslog_cfg: dict[str, Any] | None,
    webhook_cfg: dict[str, Any] | None,
) -> None:
    """Legacy path — shape-compatible with pre-multi-target callers."""
    if syslog_cfg is not None:
        await _deliver_to_target(
            {
                "kind": "syslog",
                "format": "rfc5424_json",
                "host": syslog_cfg["host"],
                "port": syslog_cfg["port"],
                "protocol": syslog_cfg["protocol"],
                "facility": syslog_cfg["facility"],
            },
            payload,
        )
    if webhook_cfg is not None:
        await _deliver_to_target(
            {
                "kind": "webhook",
                "url": webhook_cfg["url"],
                "auth_header": webhook_cfg.get("auth_header", ""),
            },
            payload,
        )


# ── Config loading ─────────────────────────────────────────────────────────


@asynccontextmanager
async def _ephemeral_session() -> AsyncIterator[AsyncSession]:
    """Short-lived engine + session for audit-forward config reads.

    Why: the ``after_commit`` listener runs on whatever event loop
    committed the parent session. In FastAPI that's the long-lived
    request loop; in Celery workers each task spins its own loop via
    ``asyncio.run``. Using the global engine from ``app.db`` would
    reuse asyncpg connections created on a prior loop and race them
    ("another operation is in progress"). An ephemeral engine with
    ``NullPool`` has no loop-bound pool state to leak.
    """
    engine = create_async_engine(_app_settings.database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def _load_forward_config() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Back-compat shim: return *one* syslog + *one* webhook config dict.

    Used by alerts.py. Prefers the first enabled row of each kind from
    ``audit_forward_target``; falls back to the flat settings columns
    when the table is empty.
    """
    async with _ephemeral_session() as session:
        res = await session.execute(
            select(AuditForwardTarget).where(AuditForwardTarget.enabled.is_(True))
        )
        rows = list(res.scalars().all())

    syslog_cfg: dict[str, Any] | None = None
    webhook_cfg: dict[str, Any] | None = None
    for t in rows:
        if syslog_cfg is None and t.kind == "syslog" and t.host:
            syslog_cfg = {
                "host": t.host,
                "port": int(t.port or 514),
                "protocol": t.protocol or "udp",
                "facility": int(t.facility or 16),
            }
        elif webhook_cfg is None and t.kind == "webhook" and t.url:
            webhook_cfg = {
                "url": t.url,
                "auth_header": t.auth_header or "",
            }
        if syslog_cfg is not None and webhook_cfg is not None:
            break

    if syslog_cfg is not None or webhook_cfg is not None:
        return syslog_cfg, webhook_cfg

    # No targets configured — fall back to legacy flat columns so a
    # pre-multi-target deployment keeps forwarding after upgrade without
    # the operator having to re-create the row.
    async with _ephemeral_session() as session:
        ps = await session.get(PlatformSettings, _SINGLETON_ID)
    if ps is None:
        return None, None
    if (
        ps.audit_forward_syslog_enabled
        and ps.audit_forward_syslog_host
        and ps.audit_forward_syslog_port
    ):
        syslog_cfg = {
            "host": ps.audit_forward_syslog_host,
            "port": int(ps.audit_forward_syslog_port),
            "protocol": ps.audit_forward_syslog_protocol or "udp",
            "facility": int(ps.audit_forward_syslog_facility),
        }
    if ps.audit_forward_webhook_enabled and ps.audit_forward_webhook_url:
        webhook_cfg = {
            "url": ps.audit_forward_webhook_url,
            "auth_header": ps.audit_forward_webhook_auth_header or "",
        }
    return syslog_cfg, webhook_cfg


async def _load_targets() -> list[dict[str, Any]]:
    """Return every enabled target as a dict. Includes a fallback from
    the legacy flat settings when the targets table is empty, so existing
    deployments keep forwarding across the upgrade boundary."""
    async with _ephemeral_session() as session:
        res = await session.execute(
            select(AuditForwardTarget).where(AuditForwardTarget.enabled.is_(True))
        )
        rows = list(res.scalars().all())

    out: list[dict[str, Any]] = []
    for t in rows:
        if t.kind == "syslog" and t.host:
            out.append(
                {
                    "name": t.name,
                    "kind": "syslog",
                    "format": t.format,
                    "host": t.host,
                    "port": int(t.port),
                    "protocol": t.protocol,
                    "facility": int(t.facility),
                    "ca_cert_pem": t.ca_cert_pem,
                    "min_severity": t.min_severity,
                    "resource_types": t.resource_types,
                }
            )
        elif t.kind == "webhook" and t.url:
            out.append(
                {
                    "name": t.name,
                    "kind": "webhook",
                    "url": t.url,
                    "auth_header": t.auth_header or "",
                    "min_severity": t.min_severity,
                    "resource_types": t.resource_types,
                }
            )
    if out:
        return out

    # Legacy flat-config fallback.
    syslog_cfg, webhook_cfg = await _load_forward_config()
    if syslog_cfg is not None:
        out.append(
            {
                "name": "Legacy Syslog",
                "kind": "syslog",
                "format": "rfc5424_json",
                **syslog_cfg,
                "ca_cert_pem": None,
                "min_severity": None,
                "resource_types": None,
            }
        )
    if webhook_cfg is not None:
        out.append(
            {
                "name": "Legacy Webhook",
                "kind": "webhook",
                **webhook_cfg,
                "min_severity": None,
                "resource_types": None,
            }
        )
    return out


# ── Dispatch ───────────────────────────────────────────────────────────────


async def _dispatch(rows: list[dict[str, Any]]) -> None:
    targets = await _load_targets()
    if not targets:
        return

    tasks: list[Any] = []
    for r in rows:
        payload = r["payload"]
        for t in targets:
            tasks.append(_deliver_to_target(t, payload))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── Listener wiring ────────────────────────────────────────────────────────


def _register_session_listener() -> None:
    """Install the ``after_flush`` + ``after_commit`` listeners.

    Runs once at import time — idempotent because SQLAlchemy's event
    system de-dups listener identity.
    """

    @event.listens_for(AsyncSession.sync_session_class, "after_flush")
    def _after_flush(session: Any, flush_context: Any) -> None:  # noqa: ARG001
        new_audits = [obj for obj in session.new if isinstance(obj, AuditLog)]
        if not new_audits:
            return
        snapshots = getattr(session, _PENDING_ATTR, None) or []
        for row in new_audits:
            snapshots.append(
                {
                    "payload": _serialize(row),
                    "result": row.result,
                    "timestamp": getattr(row, "timestamp", None) or datetime.now(UTC),
                }
            )
        setattr(session, _PENDING_ATTR, snapshots)

    @event.listens_for(AsyncSession.sync_session_class, "after_commit")
    def _after_commit(session: Any) -> None:
        snapshots = getattr(session, _PENDING_ATTR, None)
        if not snapshots:
            return
        setattr(session, _PENDING_ATTR, [])
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("audit_forward_no_loop_dropped", count=len(snapshots))
            return
        loop.create_task(_dispatch(snapshots))

    @event.listens_for(AsyncSession.sync_session_class, "after_rollback")
    def _after_rollback(session: Any) -> None:
        if getattr(session, _PENDING_ATTR, None):
            setattr(session, _PENDING_ATTR, [])


_register_session_listener()


__all__: list[str] = [
    "render_for_target",
    "_send_syslog",
    "_send_webhook",
    "_deliver_to_target",
    "_load_targets",
]
