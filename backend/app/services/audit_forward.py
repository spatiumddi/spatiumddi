"""External forwarding for audit-log events.

Subscribes to the SQLAlchemy ``after_commit`` lifecycle on every async
session and, for each successfully committed ``AuditLog`` row, fires
the configured delivery channels (RFC 5424 syslog and / or HTTP
webhook) as a background ``asyncio`` task.

Design notes:

* **Never blocks the commit.** The hook collects audit rows inside
  ``after_flush`` while they still have IDs, then schedules delivery
  in ``after_commit`` via ``asyncio.create_task`` — audit writes are
  the control-plane hot path and must stay fast even when the
  collector is unreachable.
* **Settings snapshot.** Each delivery pass reads ``PlatformSettings``
  row 1 through a short-lived session so cadence / target changes
  from the UI take effect immediately without a worker restart.
* **Single instance per process.** The listener is registered exactly
  once at import time. Multiple uvicorn workers each install their
  own copy — that's fine because each writes from its own sessions.
* **Delivery is best-effort.** Syslog over UDP can drop without
  notice, which is the RFC contract; TCP errors + webhook non-2xx
  are logged via structlog and otherwise swallowed.

See ``docs/OBSERVABILITY.md`` for the operator-facing view.
"""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.audit import AuditLog
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

# The session attribute we stash pending audit rows on between
# ``after_flush`` and ``after_commit``. Using a dunder-prefixed name so
# it's clearly ours and won't collide with application code.
_PENDING_ATTR = "__spatium_audit_forward_pending__"


# ── Event payload ──────────────────────────────────────────────────────────


def _serialize(row: AuditLog) -> dict[str, Any]:
    """Neutral JSON shape sent to both syslog and webhook.

    Kept deliberately small — collectors typically parse this into
    structured fields for search / alerting. Fields that may be very
    large (``old_value`` / ``new_value``) are passed through but we
    trust operators to configure log rotation on the collector side.
    """
    # AuditLog uses ``timestamp`` (server-side default NOW()) rather than
    # ``created_at`` — unlike most other models in the schema. Fall back to
    # wall-clock ``now`` if the DB hasn't populated it yet (e.g. during
    # tests that don't flush before emitting).
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


def _severity_for(row: AuditLog) -> int:
    r = (row.result or "").lower()
    if r == "denied":
        return _SEVERITY_DENIED
    if r in ("failed", "error"):
        return _SEVERITY_FAILED
    return _SEVERITY_SUCCESS


def _hostname() -> str:
    # Kept fresh on every call — containers often rotate hostnames on
    # restart, and the cost is negligible.
    return socket.gethostname() or "spatiumddi"


def _render_rfc5424(facility: int, row: AuditLog, payload: dict[str, Any]) -> str:
    """Format one audit row as a single RFC 5424 syslog line.

    Payload goes into the MSG body as compact JSON — most SIEMs
    (Splunk, Elastic, Graylog) detect JSON in MSG automatically and
    parse it out. STRUCTURED-DATA is set to ``-`` because we don't
    have a registered PEN; operators who want structured sdata can
    switch to the webhook.
    """
    severity = _severity_for(row)
    pri = (facility << 3) | severity
    ts_src = getattr(row, "timestamp", None) or datetime.now(UTC)
    ts = ts_src.isoformat()
    procid = "-"
    return f"<{pri}>1 {ts} {_hostname()} {_APP_NAME} {procid} {_MSG_ID} - " + json.dumps(
        payload, separators=(",", ":"), default=str
    )


# ── Delivery ───────────────────────────────────────────────────────────────


async def _send_syslog(host: str, port: int, protocol: str, message: str) -> None:
    # We open a fresh connection per-event. A long-lived TCP connection
    # would be more efficient but sharing state across asyncio tasks in
    # the listener context is complicated and rarely worth it —
    # collectors handle reconnect cheaply.
    if protocol == "udp":
        # ``socket.sendto`` is synchronous but fire-and-forget against
        # a local / LAN collector — no kernel backpressure to speak of.
        # Wrapping in ``to_thread`` is overkill here; one syscall.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto((message + "\n").encode("utf-8"), (host, port))
        return
    # TCP path — uses asyncio streams to avoid blocking the event loop
    # if the collector is slow / tarpitted.
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
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
        # ``reader`` is closed via ``writer`` — explicit drain above.
        del reader


async def _send_webhook(url: str, auth_header: str, payload: dict[str, Any]) -> None:
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        # Non-2xx is logged but never raised — the audit commit is
        # already done; retrying here would only add complexity. A
        # future "dead letter" table could capture these if it becomes
        # a real operational need.
        if resp.status_code >= 300:
            logger.warning(
                "audit_forward_webhook_non2xx",
                status=resp.status_code,
                body_preview=resp.text[:200],
            )


async def _deliver_one(
    payload: dict[str, Any],
    row_summary: dict[str, Any],
    syslog_cfg: dict[str, Any] | None,
    webhook_cfg: dict[str, Any] | None,
) -> None:
    """Deliver one audit row to all configured channels.

    Runs inside ``asyncio.create_task`` so exceptions don't propagate
    back into the caller's commit path. Each channel's failure is
    isolated — a dead syslog target shouldn't stop the webhook.
    """
    if syslog_cfg is not None:
        message = _render_rfc5424(
            syslog_cfg["facility"],
            cast(AuditLog, _StubRow(**row_summary)),
            payload,
        )
        try:
            await _send_syslog(
                syslog_cfg["host"],
                syslog_cfg["port"],
                syslog_cfg["protocol"],
                message,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audit_forward_syslog_failed",
                host=syslog_cfg["host"],
                port=syslog_cfg["port"],
                protocol=syslog_cfg["protocol"],
                error=str(exc),
            )

    if webhook_cfg is not None:
        try:
            await _send_webhook(webhook_cfg["url"], webhook_cfg["auth_header"], payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audit_forward_webhook_failed",
                url=webhook_cfg["url"],
                error=str(exc),
            )


class _StubRow:
    """Minimal duck-type of ``AuditLog`` for ``_render_rfc5424`` severity +
    timestamp resolution after the ORM session is gone.

    We can't re-use the detached ORM instance across the task boundary
    (SQLAlchemy gets cranky about expired attributes on a closed
    session), so we snapshot the fields we need into a plain object.
    """

    def __init__(self, result: str | None = None, timestamp: datetime | None = None) -> None:
        self.result = result
        self.timestamp = timestamp


# ── Listener wiring ────────────────────────────────────────────────────────


async def _load_forward_config() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read the current platform settings for forwarding.

    Returns (syslog_cfg, webhook_cfg) — either can be ``None`` when
    disabled or incompletely configured.
    """
    async with AsyncSessionLocal() as session:
        ps = await session.get(PlatformSettings, _SINGLETON_ID)
    if ps is None:
        return None, None

    syslog_cfg: dict[str, Any] | None = None
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

    webhook_cfg: dict[str, Any] | None = None
    if ps.audit_forward_webhook_enabled and ps.audit_forward_webhook_url:
        webhook_cfg = {
            "url": ps.audit_forward_webhook_url,
            "auth_header": ps.audit_forward_webhook_auth_header or "",
        }

    return syslog_cfg, webhook_cfg


async def _dispatch(rows: list[dict[str, Any]]) -> None:
    syslog_cfg, webhook_cfg = await _load_forward_config()
    if syslog_cfg is None and webhook_cfg is None:
        return
    # One task per row so slow collectors don't serialize the queue.
    # Small batches typical — admin-triggered mutations are the usual
    # source.
    await asyncio.gather(
        *[
            _deliver_one(
                r["payload"],
                {"result": r["result"], "timestamp": r["timestamp"]},
                syslog_cfg,
                webhook_cfg,
            )
            for r in rows
        ],
        return_exceptions=True,
    )


def _register_session_listener() -> None:
    """Install the ``after_flush`` + ``after_commit`` listeners.

    Runs once at import time — idempotent because SQLAlchemy's event
    system de-dups listener identity.
    """

    @event.listens_for(AsyncSession.sync_session_class, "after_flush")
    def _after_flush(session: Any, flush_context: Any) -> None:  # noqa: ARG001
        # Collect new AuditLog rows *now* so we still have the committed
        # values; walking session.new is only safe inside flush.
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
        # Clear before scheduling so an explicit re-flush in a unit-
        # of-work pattern doesn't double-fire.
        setattr(session, _PENDING_ATTR, [])
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Rare path — a sync commit (e.g. during tests / migrations)
            # runs outside an event loop. We drop the forward rather
            # than try to bootstrap a loop mid-commit; the audit row
            # still lands in the DB, which is what matters.
            logger.debug("audit_forward_no_loop_dropped", count=len(snapshots))
            return
        # Fire-and-forget — never await; never block the commit.
        loop.create_task(_dispatch(snapshots))

    @event.listens_for(AsyncSession.sync_session_class, "after_rollback")
    def _after_rollback(session: Any) -> None:
        # If the commit failed, drop the pending snapshots so a later
        # successful commit on the same session doesn't replay them.
        if getattr(session, _PENDING_ATTR, None):
            setattr(session, _PENDING_ATTR, [])


_register_session_listener()


__all__: list[str] = []
