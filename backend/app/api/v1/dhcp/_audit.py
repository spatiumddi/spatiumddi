"""Shared audit-log helper for DHCP routes.

Writes an AuditLog row before commit, per CLAUDE.md non-negotiable #4.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.auth import User


def write_audit(
    db: AsyncSession,
    *,
    user: User | None,
    action: str,
    resource_type: str,
    resource_id: str,
    resource_display: str,
    changed_fields: list[str] | None = None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    result: str = "success",
) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            user_display_name=user.display_name if user else "system",
            auth_source=user.auth_source if user else "system",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_display=resource_display,
            changed_fields=changed_fields,
            old_value=old_value,
            new_value=new_value,
            result=result,
        )
    )
